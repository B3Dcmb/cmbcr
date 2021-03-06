from __future__ import division
import numpy as np
from scipy.sparse import dok_matrix
import scipy

from .beams import standard_needlet_by_l, fourth_order_beam, gaussian_beam_by_l
from . import sharp
from .cg import cg_generator
from .utils import scatter_l_to_lm, hammer
from .cache import memory


def needletify_dl(b, lmax_factor, dl):
    lmax = int(lmax_factor * dl.shape[0]) - 1
    if 1:
        nl = standard_needlet_by_l(b, lmax)
        i = nl.argmax()
        return np.concatenate([dl, nl[i:] * dl[-1] / nl[i]])
    else:
        # FAILED!
        nl = fourth_order_beam(lmax, int(0.1 * lmax))
        return np.concatenate([dl, nl * dl[-1] / nl[0]])
        


def operator_image_to_power_spectrum(unitvec, opimage):
    # unitvec: unit-vector in flatsky basis
    # x: image of operator
    FtW_x = flatsky_analysis(opimage)
    Ft_u = flatsky_adjoint_synthesis(unitvec)
    dl_fft = np.abs(FtW_x / Ft_u)
    return dl_fft


def sphere_dl_to_2d_fft_spectrum(dl, ntheta, nphi):
    """
    Convert the operator Y D1 Y^T to F D2 F^T, where Y are spherical harmonics,
    D1 has `dl` on the diagonal, and D2. The result will be on the resolution given by ntheta, nphi.
    """
    nrings = dl.shape[0]

    unitvec_hi = np.zeros((nrings, 2 * nrings))
    unitvec_hi[nrings // 2, nrings] = 1

    # Make a [2*pi, pi] image of the operator on the equator using spherical harmonics, at the full resolution
    # supporting lmax
    u = sharp.sh_adjoint_synthesis_gauss(nrings - 1, unitvec_hi.reshape(2 * nrings**2))
    u *= scatter_l_to_lm(dl)
    opimage_hi = sharp.sh_synthesis_gauss(nrings - 1, u).reshape((nrings, 2 * nrings))

    # Slice out a piece that is large enough to only support (ntheta, nphi)
    opimage_lo = opimage_hi[(nrings - ntheta) // 2:(nrings + ntheta) // 2, (2 * nrings - nphi) // 2:(2 * nrings + nphi) // 2]
    unitvec_lo = unitvec_hi[(nrings - ntheta) // 2:(nrings + ntheta) // 2, (2 * nrings - nphi) // 2:(2 * nrings + nphi) // 2]

    # Turn it into Fourier power spectrum
    result = operator_image_to_power_spectrum(unitvec_lo, opimage_lo)
    assert result.shape == (ntheta, nphi)
    return result



def flatsky_analysis(u):
    return np.fft.fftn(u) / np.prod(u.shape)


def flatsky_adjoint_analysis(u):
    return np.fft.ifftn(u)


def flatsky_synthesis(u):
    return np.fft.ifftn(u) * np.prod(u.shape)


def flatsky_adjoint_synthesis(u):
    return np.fft.fftn(u)


@memory.cache
def coarsen_matrix(ntheta, nphi):
    coarse_ntheta = ntheta // 2
    coarse_nphi = nphi // 2
    R = dok_matrix((coarse_ntheta * coarse_nphi, ntheta * nphi))

    def add(coarse_i, coarse_j, fine_i, fine_j, weight):
        # wrap around fine_i and fine_j..
        fine_i = fine_i % ntheta
        fine_j = fine_j % nphi

        coarse_ind = coarse_i * coarse_nphi + coarse_j
        fine_ind = fine_i * nphi + fine_j
        R[coarse_ind, fine_ind] = weight
    
    for i in range(coarse_ntheta):
        for j in range(coarse_nphi):
            # row above
            add(i, j, 2 * i - 1, 2 * j - 1, 1/16.)
            add(i, j, 2 * i - 1, 2 * j, 1/8.)
            add(i, j, 2 * i - 1, 2 * j + 1, 1/16.)

            # center row
            add(i, j, 2 * i, 2 * j - 1, 1/8.)
            add(i, j, 2 * i, 2 * j, 1/4.)
            add(i, j, 2 * i, 2 * j + 1, 1/8.)

            # row below
            add(i, j, 2 * i + 1, 2 * j - 1, 1/16.)
            add(i, j, 2 * i + 1, 2 * j, 1/8.)
            add(i, j, 2 * i + 1, 2 * j + 1, 1/16.)

    return R.tocsr()


class SinvSolver(object):

    def __init__(self, dl, mask_gauss, split=False):
        self.nrings = int(np.round(np.sqrt(mask_gauss.shape[0] / 2)))
        assert mask_gauss.shape[0] == 2 * self.nrings**2
        self.lmax = self.nrings - 1

        self.start_ring = 3 * self.nrings // 8
        self.stop_ring = 5 * self.nrings // 8

        self.dl = dl
        
        self.lmax_sh = self.dl.shape[0] - 1

        self.dl = dl
        self.mask = self.gauss_grid_to_equator(mask_gauss)

        # Transfer the S^{-1} operator from spherical harmonics to Fourier
        ntheta = self.stop_ring - self.start_ring
        nphi = 2 * self.nrings
        self.shape = (ntheta, nphi)

        self.outer_dl_fft = sphere_dl_to_2d_fft_spectrum(self.dl, ntheta, nphi)
        
        #self.outer_dl_fft *= img[0,0] / self.outer_dl_fft[0,0]

        self.split = split
        if self.split:
            self.inner_dl_fft = np.sqrt(self.outer_dl_fft)
        else:
            self.inner_dl_fft = self.outer_dl_fft
            
        # Build multi-grid levels
        cur_level = Level(self.inner_dl_fft, self.mask)
        self.levels = [cur_level]

        while cur_level.n > 50:
            cur_level = coarsen_level(cur_level)
            self.levels.append(cur_level)
    
        self.smoothers = [DiagonalSmoother(lev) for lev in self.levels][:-1]
        self.smoothers.append(DenseSmoother(self.levels[-1]))


        self.n = int((self.mask == 0).sum())
        

    def restrict(self, u, lmax=None):
        lmax = lmax or self.lmax
        return self.pickvec(self.gauss_grid_to_equator(sharp.sh_synthesis_gauss(self.nrings - 1, u, lmax_sh=lmax)))

    def prolong(self, u, lmax=None):
        lmax = lmax or self.lmax
        return sharp.sh_adjoint_synthesis_gauss(self.nrings - 1, self.equator_to_gauss_grid(self.padvec(u)), lmax_sh=lmax)
            
    def pickvec(self, u):
        return self.levels[0].pickvec(u)

    def padvec(self, u):
        return self.levels[0].padvec(u)

    def outer_matvec(self, u_in):
        root_level = self.levels[0]
        u = root_level.padvec(u_in)
        u = flatsky_adjoint_synthesis(u)
        u *= self.outer_dl_fft
        u = flatsky_synthesis(u)
        u = root_level.pickvec(u).real
        return u

    def outer_precond(self, b):
        x = self.inner_precond(b)
        if self.split:
            x = self.inner_precond(x)
        return x

    def inner_precond(self, b):
        return v_cycle(0, self.levels, self.smoothers, b)

    def equator_to_gauss_grid(self, u):
        u_pad = np.zeros((self.nrings, 2 * self.nrings))
        u_pad[self.start_ring:self.stop_ring, :] = u.reshape(self.shape)
        return u_pad.reshape(2 * self.nrings**2)

    def gauss_grid_to_equator(self, u):
        u = u.reshape((self.nrings, 2 * self.nrings))
        return u[self.start_ring:self.stop_ring, :]

    def solve_mask(self, b, x0=None, rtol=1e-6, maxit=50):
        """
        Returns (x, reslst, errlst)

        If x0 is supplied, compute the errors and return errlst; otherwise errlst is empt
        """
        solver = cg_generator(
            self.outer_matvec,
            b=b,
            M=self.outer_precond,
            x0=np.zeros_like(b)
            )

        reslst = []
        errlst = []
        if x0 is not None:
            x0_norm = np.linalg.norm(x0)
        b_norm = np.linalg.norm(b)

        for i, (x, r, delta_new) in enumerate(solver):
            r = np.linalg.norm(r) / b_norm
            reslst.append(r)
            if x0 is not None:
                e = np.linalg.norm(x0 - x) / x0_norm
                errlst.append(e)
                print 'iteration {}, res={}, err={}'.format(i, r, e)
            else:
                pass
                print 'iteration {}, res={}'.format(i, r)
            if r < rtol or i > maxit:
                print 'breaking', r, repr(rtol), i, maxit
                break

        return x, reslst, errlst

    def solve_alm(self, b, single_v_cycle=False, repeat=1, *args, **kw):
        1/0
        x = self.pickvec(self.gauss_grid_to_equator(sharp.sh_synthesis_gauss(self.nrings - 1, b, lmax_sh=self.lmax_sh)))
        for i in range(repeat):
            if single_v_cycle:
                x = self.outer_precond(x)
            else:
                x, reslst, errlst = self.solve_mask(x, *args, **kw)
        x = sharp.sh_adjoint_synthesis_gauss(self.nrings - 1, self.equator_to_gauss_grid(self.padvec(x)), lmax_sh=self.lmax_sh)
        return x
    
    

class Level(object):
    def __init__(self, dl_fft, mask):
        self.mask = mask
        self.dl_fft = dl_fft
        self.ntheta, self.nphi = dl_fft.shape
        self.pick = (mask.reshape(self.ntheta * self.nphi) == 0)
        self.n = int(self.pick.sum())
        self.R = coarsen_matrix(self.ntheta, self.nphi)
        self.ntheta_H = self.ntheta // 2
        self.nphi_H = self.nphi // 2

    def compute_diagonal(self):
        # sample the operator to figure out the constant to use...
        u = np.zeros((self.ntheta, self.nphi))
        u[self.ntheta // 2, 0] = 1
        u = self.matvec_padded(u)
        return u[self.ntheta // 2, 0]
        
    def pickvec(self, u):
        return u.reshape(self.ntheta * self.nphi)[self.pick]

    def padvec(self, u):
        u_pad = np.zeros(self.ntheta * self.nphi)
        u_pad[self.pick] = u.real
        return u_pad.reshape(self.ntheta, self.nphi)

    def matvec_padded(self, u):
        u = flatsky_adjoint_synthesis(u)
        u *= self.dl_fft
        u = flatsky_synthesis(u).real
        return u
    
    def matvec(self, u_in):
        u = self.padvec(u_in)
        u = self.matvec_padded(u)
        u = self.pickvec(u)
        return u

    def matvec_coarsened(self, u):
        # do matvec on the next, coarser level. This is just done once, to create the operator on the next level
        return self.coarsen_padded(self.matvec_padded(self.interpolate_padded(u)))

    def coarsen_padded(self, u):
        return (self.R * u.reshape(self.ntheta * self.nphi)).reshape(self.ntheta_H, self.nphi_H)

    def interpolate_padded(self, u):
        return (self.R.T * u.reshape(self.ntheta_H * self.nphi_H)).reshape(self.ntheta, self.nphi)


class DenseSmoother(object):
    def __init__(self, level):
        self.matrix = hammer(level.matvec, level.n)
        self.inv_matrix = np.linalg.inv(self.matrix)

    def apply(self, u):
        return np.dot(self.inv_matrix, u)

                                 
class DiagonalSmoother(object):
    def __init__(self, level):
        self.level = level

        self.diag = level.compute_diagonal()
        self.inv_diag = 1 / self.diag

    def apply(self, u):
        return 0.3 * self.inv_diag * u


def v_cycle(ilevel, levels, smoothers, b):
    if ilevel == len(levels) - 1:
        return smoothers[ilevel].apply(b)
    else:
        level = levels[ilevel]
        next_level = levels[ilevel + 1]

        x = b * 0
        for i in range(1):
            x += smoothers[ilevel].apply(b - level.matvec(x))

        for i in range(1):
            r_h = b - level.matvec(x)

            r_H = coarsen(level, next_level, r_h)

            c_H = v_cycle(ilevel + 1, levels, smoothers, r_H)

            c_h = interpolate(level, next_level, c_H)
            
            x += c_h

        for i in range(1):
            x += smoothers[ilevel].apply(b - level.matvec(x))
        return x


def coarsen(level, next_level, u):
    return next_level.pickvec(level.coarsen_padded(level.padvec(u)))

def interpolate(level, next_level, u):
    return level.pickvec(level.interpolate_padded(next_level.padvec(u)))    

def coarsen_level(level):
    # produce next coarser level
    ntheta_H = level.ntheta // 2
    nphi_H = level.nphi // 2
    mask_H = level.coarsen_padded(level.mask)
    mask_H[mask_H < 0.5] = 0
    mask_H[mask_H != 0] = 1

    unitvec = np.zeros((ntheta_H, nphi_H))
    unitvec[ntheta_H // 2, nphi_H // 2] = 1
    image_of_operator = level.matvec_coarsened(unitvec)
    dl_fft_H = operator_image_to_power_spectrum(unitvec, image_of_operator)
    return Level(dl_fft_H, mask_H)

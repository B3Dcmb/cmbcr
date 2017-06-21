import numpy as np

from .mblocks import gauss_ring_map_to_phase_map
from .harmonic_preconditioner import factor_banded_preconditioner
from .harmonic_preconditioner import solve_banded_preconditioner
from .harmonic_preconditioner import construct_banded_preconditioner
from .harmonic_preconditioner import k_kp_idx
from .utils import pad_or_trunc, timed, pad_or_truncate_alm, scatter_l_to_lm

__all__ = ['BandedHarmonicPreconditioner']


class BandedHarmonicPreconditioner(object):
    def __init__(self, system, diagonal=False, couplings=True, factor=True):
        lmax = max(system.lmax_list)
        
        precond_data = np.zeros((5 * system.comp_count, system.comp_count * (lmax + 1)**2), dtype=np.float32, order='F')
        Ni_diag = 0

        for nu in range(system.band_count):

            #ninv_phase_maps = np.zeros(
            #    (2 * lmax + 1, system.lmax_ninv + 1, (system.comp_count * (system.comp_count + 1)) // 2),
            #    order='F', dtype=np.complex128)

            ninv_phase, thetas = gauss_ring_map_to_phase_map(system.ninv_gauss_lst[nu], system.lmax_ninv, lmax)
            #assert thetas.shape[0] == ninv_phase_maps.shape[1]

            with timed('construct_banded_preconditioner {}'.format(nu)):
                construct_banded_preconditioner(
                    lmax,
                    system.comp_count,
                    thetas,
                    ninv_phase.copy('F'),
                    bl=system.bl_list[nu][:lmax + 1],
                    mixing_scalars=system.mixing_scalars[nu, :].copy(),
                    out=precond_data)

        for k in range(system.comp_count):
            precond_data[0, k::system.comp_count] += scatter_l_to_lm(pad_or_trunc(system.dl_list[k], lmax + 1))
            
        if diagonal:
            precond_data[1:, :] = 0

        if factor:
            factor_banded_preconditioner(lmax, system.comp_count, precond_data)

        self.system = system
        self.data = precond_data
        self.lmax = lmax


    def apply(self, x_lst):
        comp_count = self.system.comp_count

        buf = np.empty(((self.lmax + 1)**2, comp_count), order='F', dtype=np.float32)
        for k in range(comp_count):
            buf[:, k] = pad_or_truncate_alm(x_lst[k], self.lmax)

        buf = solve_banded_preconditioner(self.lmax, comp_count, self.data, buf)

        result = [None] * comp_count
        for k in range(comp_count):
            result[k] = pad_or_truncate_alm(buf[:, k], self.system.lmax_list[k]).astype(np.double)

        return result
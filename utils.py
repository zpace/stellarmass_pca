import numpy as np
import matplotlib.pyplot as plt
import pickle as pkl

from astropy import units as u, constants as c, table as t
from specutils import extinction
from speclite import redshift as slrs
from speclite import accumulate as slacc

from scipy.ndimage.filters import gaussian_filter1d as gf
from scipy.interpolate import interp1d
from scipy.spatial.distance import pdist, squareform

import multiprocessing as mpc
import ctypes

import os
import sys

# add manga RC location to path, and import config
if os.environ['MANGA_CONFIG_LOC'] not in sys.path:
    sys.path.append(os.environ['MANGA_CONFIG_LOC'])

import mangarc

if mangarc.tools_loc not in sys.path:
    sys.path.append(mangarc.tools_loc)

# personal
import manga_tools as m

ln10 = np.log(10.)
mpl_v = 'MPL-5'


class GaussPeak(object):
    def __init__(self, pos, wid, ampl=None, flux=None):
        self.pos = pos
        self.wid = wid

        if (not ampl) and (not flux):
            self.ampl = 1.
        elif not flux:
            pass
        elif not ampl:
            self.ampl = 1. / (self.wid * np.sqrt(2. * np.pi))
        else:
            raise ValueError('specify either or none of (ampl, flux)')

    @property
    def flux(self):
        return self.ampl * self.wid * np.sqrt(2. * np.pi)

    def __call__(self, x):
        return self.ampl * np.exp(-(x - self.pos)**2. / (2. * self.wid))


def multigaussflux(POSs, WIDs, FLUXs, x):
    peaks = [GaussPeak(pos=pos, wid=wid, flux=flux)
             for (pos, wid, flux) in zip(POSs, WIDs, FLUXs)]
    res = np.add.reduce([peak(x) for peak in peaks])

    return res

def lsf2cov(f, l, dl):
    '''
    turn a line-spread function into a covariance matrix

    params:
     - l: wavelength array
     - dl: wavelength width subtended by each wavelength pixel
    '''
    w = f(l)

    # divide each dl by width of kernel: this yields some measure of
    # distance from l->l', normalized to width of LSF at that wavelength
    w_dl = w / dl

    # rescale l, so l(i+1) = l(i) + dl_w(i)
    l_resc = np.cumsum(w_dl)

    # make a distance matrix in the rescaled-l space
    DD = squareform(pdist(l_resc[:, None]))
    K = np.exp(-0.5 * DD**2.)

    return K, DD, w


class MaNGA_LSF(object):
    '''
    '''
    def __init__(self, LSF_R_obs_gpr, **kwargs):
        self.LSF_R_obs_gpr = LSF_R_obs_gpr

    def LSF_pix_z(self, lam, dlogl, z):
        '''
        calculate width (pix) of LSF
        '''
        specres = self.LSF_R_obs_gpr.predict(
            np.atleast_2d(lam).T)
        dlnl = dlogl * ln10

        wpix = (1. / dlnl) * (1. / specres)
        wpix_z = wpix / (1. + z)

        return wpix_z

    @classmethod
    def from_drpall(cls, drpall, n=None, **kwargs):
        '''
        read in lots of IFUs' LSFs, assume a redshift
        '''
        import sklearn.gaussian_process as gp

        if n is None:
            n = len(drpall)

        lam, specres, dspecres = zip(
            *[m.hdu_data_extract(
                  hdulist=m.load_drp_logcube(
                      plate=row['plate'], ifu=row['ifudsgn'], mpl_v=mpl_v),
                  names=['WAVE', 'SPECRES', 'SPECRESD'])
              for row in drpall[:n]])

        lam = np.concatenate(lam)
        specres = np.concatenate(specres)
        dspecres = np.concatenate(dspecres)
        good = np.logical_and.reduce(
            list(map(np.isfinite, [lam, specres, dspecres])))
        lam, specres, dspecres = lam[good], specres[good], dspecres[good]

        regressor = gp.GaussianProcessRegressor(alpha=dspecres)
        regressor.fit(X=np.atleast_2d(lam).T, y=specres)

        return cls(LSF_pix_obs=wpix, **kwargs)

    def __call__(self, lam, dlogl, y, z):
        '''
        performs convolution with LSF appropriate to a given redshift
        '''
        wpix_z = self.LSF_pix_z(lam, dlogl, z)
        yfilt = np.row_stack(
            [gaussian_filter(spec=s, sig=wpix_z)] for s in y)
        return yfilt


class KPCGen(object):
    '''
    compute some spaxel's PC cov matrix
    '''

    def __init__(self, kspec_obs, i0_map, E, ivar_scaled):
        self.kspec_obs = kspec_obs
        self.i0_map = i0_map
        self.E = E
        self.q, self.nl = E.shape
        self.ivar_scaled = ivar_scaled

    def __call__(self, i, j):
        i0_ = self.i0_map[i, j]
        sl = [slice(i0_, i0_ + self.nl) for _ in range(2)]
        kspec = self.kspec_obs[sl]
        # add to diagonal term from actual variance, floored at .1%
        var = 1. / self.ivar_scaled[..., i, j]
        np.einsum('ii->i', kspec)[:] = 1. / self.ivar_scaled[..., i, j].clip(
            min=1.0e-6, max=1.0e6)
        return (self.E @ (kspec) @ self.E.T)


def interp_large(x0, y0, xnew, axis, nchunks=1, **kwargs):
    '''
    large-array-tolerant interpolation
    '''

    success = False

    specs_interp = interp1d(x=x0, y=y0, axis=axis, **kwargs)

    while not success:
        # chunkify x array
        xchunks = np.array_split(xnew, nchunks)
        try:
            ynew = np.concatenate(
                [specs_interp(xc) for xc in xchunks], axis=axis)
        except MemoryError:
            nchunks += 1
        else:
            success = True

    return ynew

def _nearest(loglgrid, loglrest, frest, ivarfrest):
    '''
    workhorse integer-pixel function
    '''

    # how to index full cube
    L, I, J = np.ix_(*[range(i) for i in frest.shape])

    # reference index and corresponding loglam **IN GRID**
    # this avoids latching onto endpoints erroneously
    grid_nl = len(loglgrid)
    ix_ref_grid = (grid_nl - 1) // 2
    logl_ref_grid = loglgrid[ix_ref_grid]

    ix_ref_rest = np.argmin(np.abs(loglrest - logl_ref_grid), axis=0)
    logl_ref_rest = loglrest[ix_ref_rest, I, J].squeeze()

    NX, NY = ix_ref_rest.shape
    xctr, yctr = NX // 2, NY // 2

    dlogl_resid = (logl_ref_rest - logl_ref_grid)

    '''
    set up what amounts to a linear mapping from rest to rectified,
        from: pixel u of spaxel I, J of rest-frame cube
        to:   pixel u' of spaxel I', J' of rectified cube

    in reality pixel ix_ref_grid - 1 <==> ix_ref_rest - 1
                     ix_ref_grid     <==> ix_ref_rest
                     ix-ref_grid + 1 <==> ix_ref_rest + 1
                     etc...
    '''

    def rect2rest(i0_rect, i0_rest, i_rect):
        '''
        maps pixel number (rectified frame) to pixel number (rest frame),
            assuming that each pixel is the same width
        '''
        # delta no. pix from reference in rect frame
        d_rect = i_rect - i0_rect
        return i0_rest + d_rect + 1 # should this be -1, 0, or 1?

    # create array of individual wavelength indices in rectified array
    l_ixs_rect = np.linspace(0, grid_nl - 1, grid_nl, dtype=int)[:, None, None]
    l_ixs_rest = rect2rest(ix_ref_grid, ix_ref_rest, l_ixs_rect)
    # where are we out of range?
    badrange = np.logical_or.reduce(((l_ixs_rest >= grid_nl),
                                     (l_ixs_rest < 0)))
    #l_ixs_rest[badrange] = 0

    flux_regr = frest[l_ixs_rest, I, J]
    ivar_regr = ivarfrest[l_ixs_rest, I, J]

    #ivar_regr[badrange] = 0.

    return flux_regr, ivar_regr, dlogl_resid

class Regridder(object):
    '''
    place regularly-sampled array onto another grid
    '''

    methods = ['nearest', 'invdistwt', 'interp', 'supersample']

    def __init__(self, loglgrid, loglrest, frest, ivarfrest, dlogl=1.0e-4):
        self.loglgrid = loglgrid
        self.loglrest = loglrest
        self.frest = frest
        self.ivarfrest = ivarfrest

        self.dlogl = dlogl

    def nearest(self):
        '''
        integer-pixel deredshifting
        '''

        loglgrid = self.loglgrid
        loglrest = self.loglrest
        frest = self.frest
        ivarfrest = self.ivarfrest

        flux_regr, ivar_regr, *_ = _nearest(
            loglgrid=loglgrid, loglrest=loglrest,
            frest=frest, ivarfrest=ivarfrest)

        return flux_regr, ivar_regr

    def invdistwt(self):
        '''
        inverse-distance-weighted deredshifting
        '''

        # do nearest-pixel deredshifting
        loglgrid = self.loglgrid
        grid_nl = len(loglgrid)
        # expand logl grid
        loglgrid_ = np.concatenate(([loglgrid[0] - self.dlogl],
                                    loglgrid,
                                    [loglgrid[-1] + self.dlogl]))
        loglrest = self.loglrest
        frest = self.frest
        ivarfrest = self.ivarfrest

        # get the "best-possible" deredshift scenario
        flux_n, ivar_n, dlogl_resid = _nearest(
            loglgrid_, loglrest, frest, ivarfrest)

        # intermediate logl: destination grid, minus residual
        logl_inter = loglgrid_[..., None, None] - dlogl_resid[None, ...]
        # residual as fraction of a pixel
        sfpix = dlogl_resid / self.dlogl

        '''
        have we deredshifted too much or too little?

        if dlogl_resid is positive, then actual solution is redward
            i.e., we have not deredshifted enough
                  so we take pixel 1 ==> nl
                  and the stated fpix is measured relative to LHS
        if dlogl_resid is negative, then actual solution is blueward
            i.e., we have deredshifted too much
                  so we take pixel 0 ==> nl - 1
                  and the stated fpix is measured relative to RHS
        '''

        lr = np.sign(sfpix).astype(int)
        reorder = (lr < 0.)
        fpix = np.abs(sfpix)

        # logl starting points: left and right
        # left point has to do with whether spectrum was deredshifted too much
        startl = np.select(condlist=[reorder, ~reorder],
                           choicelist=[np.zeros_like(fpix, dtype=int),
                                       np.ones_like(fpix, dtype=int)]) - 1
        startr = startl + 1

        # make spatial selector
        _, I, J = np.ix_(*[range(i) for i in flux_n.shape])

        # make spectral selectors
        ixs_l = (startl[None, ...] + np.arange(grid_nl)[..., None, None])
        ixs_r = (startr[None, ...] + np.arange(grid_nl)[..., None, None])

        # weights of close and far pixels
        fpix_close = fpix
        fpix_far = 1. - fpix_close

        # assemble close and far weights
        w_ = 1. / np.stack([fpix_close, fpix_far], axis=0)
        # reverse weights
        w_rev_ = w_[::-1, ...]

        # re-order according to
        # if fpix is measured relative to LHS, order is ok
        w = np.select(condlist=[reorder, ~reorder], choicelist=[w_, w_rev_])

        # construct fluxs
        fluxs_l = flux_n[ixs_l, I, J]
        fluxs_r = flux_n[ixs_r, I, J]

        fluxs = ((fluxs_l * w[0, ...]) + (fluxs_r * w[1, ...])) / w.sum(axis=0)
        ivars = 1. / ((1. / ivar_n[ixs_l, I, J].clip(min=1.0e-4)) + \
                      (1. / ivar_n[ixs_r, I, J].clip(min=1.0e-4)))
        ivars[~np.isfinite(ivars)] = 0.

        return fluxs, ivars

    def interp(self):
        pass

    def supersample(self, nper=10):
        pass

class FilterFuncs(object):
    '''
    turn a list of TableGroup-filtering functions into a single function
    '''
    def __init__(self, funcs):
        self.funcs = funcs

    def __call__(self, tab, *args):
        if (self.funcs is None):
            return True

        if len(self.funcs) == 0:
            return True

        for f in self.funcs:
            if f(tab, *args) is False:
                return False
        else:
            return True

def pickle_loader(fname):
    with open(fname, 'rb') as f:
        p = pkl.load(f)

    return p

def matcher(x, s):
    if s in x:
        return True
    else:
        return False

def lin_transform(r1, r2, x):
    '''
    transform x from range 1 to range 2
    '''

    # point-slope method
    d1 = r1[1] - r1[0]
    d2 = r2[1] - r2[0]
    px, py = r1[0], r2[0]
    m = d2 / d1

    return (x - px) * m + py

def determine_dlogl(logl):
    dlogl = np.round(np.mean(logl[1:] - logl[:-1]), 8)
    return dlogl

def gaussian_filter(spec, sig):
    '''
    variable-width convolution of a spectrum

    inspired by Michele Cappellari's similar routine

    params:
        - spec: vector of a single spectrum
        - sig: vector giving width of gaussian peak (in pixels)
    '''

    p = int(np.ceil(3. * np.max(sig)))
    m = 2 * p + 1  # kernel size
    x2 = np.linspace(-p, p, m)**2

    n = spec.size
    a = np.zeros((m, n))
    for j in range(m):   # Loop over the small size of the kernel
        a[j, p:-p] = spec[j:n - m + j + 1]

    gau = np.exp(-x2[:, None] / (2 * sig**2.))
    gau /= np.sum(gau, axis=0)[None, :]  # Normalize kernel

    f = np.sum(a * gau, axis=0)

    return f

def add_losvds(meta, spec, dlogl, vmin=10, vmax=500, nv=10, LSF=None):
    '''
    take spectra and blur each one a few times
    '''

    if LSF is None:
        LSF = np.zeros_like(spec[0, :])

    RS = np.random.RandomState()

    i_s = range(spec.shape[0])

    meta, spec = zip(*[_add_losvds_single(m, s, dlogl, vmin, vmax, nv,
                                          RS, LSF, i)
                       for m, s, i in zip(meta, spec, i_s)])
    meta = t.vstack(meta)
    spec = np.row_stack(spec)

    return meta, spec

def _add_losvds_single(meta, spec, dlogl, vmin, vmax, nv, RS, LSF, i):

    vels = RS.uniform(vmin, vmax, nv) * u.Unit('km/s')

    # dlogl is just redshift per pixel
    z_ = (vels / c.c).decompose().value
    sig = ln10 * (z_ / dlogl)
    sig = np.atleast_2d(sig).T
    sig = np.sqrt(sig**2. + LSF**2.)
    sig = sig.clip(min=.01, max=None)

    meta = t.vstack([meta, ] * len(vels))
    meta['sigma'] = vels.value

    spec = np.row_stack([gaussian_filter(spec, s) for s in sig])

    if i % 10 == 0:
        print('Done with {}'.format(i))

    return meta, spec

def weighted_quantile(p, w, q):
    '''
    like numpy.percentile, but supports weights and behaves in
        vectorized fashion. Basically computes pctls of n samples
        in (X, Y) grid

    params:
        - p: array-like, shape (n): parameter values
        - w: array-like, shape (n, X, Y): weights of parameters
        - q: array-like, shape (p): quantiles needed

    Note: assumes that map-like axes are final 2
    '''

    # sort p and w by increasing value of p
    sorter = np.argsort(p)
    p, w = p[sorter], w[sorter]

    wt_qtls = np.cumsum(w, axis=0) - 0.5 * w.sum(axis=0)
    wt_qtls /= w.sum(axis=0)

    interp = np.interp(q, wt_qtls, p)

def extinction_correct(l, f, EBV, r_v=3.1, ivar=None, **kwargs):
    '''
    wraps around specutils.extinction.reddening
    '''

    a_v = r_v * EBV
    r = extinction.reddening

    # output from reddening is inverse-flux-transmission
    f_itr = r(wave=l, a_v=a_v, r_v=r_v, **kwargs)[..., None, None]
    # to deredden, divide f by f_itr

    f /= f_itr

    if ivar is not None:
        ivar *= f_itr**2.
        return f, ivar
    else:
        return f

def extinction_correct(l, f, EBV, r_v=3.1, ivar=None, **kwargs):
    '''
    wraps around specutils.extinction.reddening
    '''

    a_v = r_v * EBV
    r = extinction.reddening

    # output from reddening is inverse-flux-transmission
    f_itr = r(wave=l, a_v=a_v, r_v=r_v, **kwargs)[..., None, None]
    # to deredden, divide f by f_itr

    f /= f_itr

    if ivar is not None:
        ivar *= f_itr**2.
        return f, ivar
    else:
        return f

def extinction_atten(l, f, EBV, r_v=3.1, ivar=None, **kwargs):
    '''
    wraps around specutils.extinction.reddening
    '''

    a_v = r_v * EBV
    r = extinction.reddening

    # output from reddening is inverse-flux-transmission
    f_itr = r(wave=l, a_v=a_v, r_v=r_v, **kwargs)
    # to redden, mult f by f_itr

    f *= f_itr

    if ivar is not None:
        ivar /= f_itr**2.
        return f, ivar
    else:
        return f

def add_redshifts(zs, axis=0):
    '''
    add redshifts in an array-like, along an axis
    '''

    z_tot = np.prod((1. + zs), axis=axis) - 1.
    return z_tot

def redshift(l, f, ivar, **kwargs):
    '''
    wraps around speclite.redshift, and does all the rule-making, etc

    assumes l, flam, ivar of flam
    '''

    s = f.shape
    l = np.tile(l[..., None, None], (1, ) + s[-2:])
    data = np.empty_like(
        f, dtype=[('l', float), ('f', float), ('ivar', float)])
    data['l'] = l
    data['f'] = f
    data['ivar'] = ivar
    rules = [dict(name='l', exponent=+1),
             dict(name='f', exponent=-1),
             dict(name='ivar', exponent=+2)]

    res = slrs(data_in=data, rules=rules, **kwargs)

    return res['l'], res['f'], res['ivar']

def coadd(f, ivar):

    fnew = f.sum(axis=(1, 2))
    ivarnew = ivar.sum(axis=(1, 2))

    return fnew, ivarnew

def PC_cov(cov, snr, i0, E, nl, q):
    if snr < 1.:
        return 100. * np.ones((q, q))
    else:
        sl = [slice(i0, i0 + nl) for _ in range(2)]
        return E @ (cov[i0 : i0 + nl, i0 : i0 + nl]) @ E.T


def reshape_cube2rss(cubes):
    '''
    reshape datacube into pseudo-RSS (spaxel-wise)
    '''

    naxis1, naxis2, naxis3 = cubes[0].shape

    # reshape data
    # axis 0: observation; axis 1: spaxel; axis 2: wavelength

    rss = np.stack([cube.reshape((naxis1, -1)).T for cube in cubes])
    return rss

def apply_mask(A, mask, axis=0):
    '''
    apply spaxel mask along axis 1
    '''

    if type(axis) is np.ndarray:
        A = (A, )

    if type(axis) is int:
        axis = (axis, ) * len(A)

    A_new = (np.compress(a=A_, condition=mask, axis=ax) for A_, ax in zip(A, axis))

    return A_new

def bin_sum_agg(A, bins):
    '''
    sum-aggregate array A along bin numbers given in `bins`
    '''
    mask = np.zeros((bins.max()+1, len(bins)), dtype=bool)
    mask[bins, np.arange(len(bins))] = 1

    return mask.dot(A)

class LogcubeDimError(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__()

hdu_unit = lambda hdu: u.Unit(hdu.header['BUNIT'])

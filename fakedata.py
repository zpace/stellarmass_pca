import numpy as np
import matplotlib.pyplot as plt

from astropy.io import fits
from astropy import units as u, constants as c, table as t
import extinction

from scipy.interpolate import interp1d
from scipy.signal import medfilt

import os, sys
from copy import copy

# local
import utils as ut
import indices
from spectrophot import Spec2Phot

from importer import *

import manga_tools as m

class SkyContamination(object):
    def __init__(self, drpall):
        sky_ifus = drpall[np.where(m.mask_from_maskbits(drpall['mngtarg2'], [1]))[0]]

        flux, ivar, lam = zip(*[self.get_ifu_sky_spectra(row) for row in sky_ifus])
        self.flux, self.ivar = np.row_stack(flux), np.row_stack(ivar)
        self.lam = lam[0]

    @classmethod
    def from_mpl_v(cls, v):
        drpall = m.load_drpall(v)
        return cls(drpall)

    def get_ifu_sky_spectra(self, row):
        drp = m.load_drp_logcube(row['plate'], row['ifudsgn'], mpl_v)
        mask = m.mask_from_maskbits(
            drp['MASK'].data, [3, 10]).astype(float).mean(axis=0) >= .1
        sp_i, sp_j = np.where(~mask)
        flux = drp['FLUX'].data[:, sp_i, sp_j].T
        ivar = drp['IVAR'].data[:, sp_i, sp_j].T
        lam = drp['WAVE'].data
        drp.close()
        return flux, ivar, lam

    def make_skycube(self, mapshape):
        ixs = np.random.randint(0, self.flux.shape[0] - 1, np.multiply(*mapshape))

        skyfluxs = self.flux[ixs, :].T.reshape((-1, ) + mapshape)
        skyivars = self.ivar[ixs, :].T.reshape((-1, ) + mapshape)

        return skyfluxs, skyivars

def noisify_cov(cov, mapshape):
    cov_noise = np.random.multivariate_normal(
        mean=np.zeros_like(np.diag(cov.cov)),
        cov=cov.cov, size=mapshape)
    cov_noise = np.moveaxis(cov_noise, [0, 1, 2], [1, 2, 0])
    return cov_noise

def compute_snrcube(flux, ivar, filtersize_l=15, return_rms_map=False):
    '''
    compute the (appx) SNR cube, and if specified, return the spaxelwise
        rms map of the cube
    '''

    snrcube = medfilt(np.abs(flux) * np.sqrt(ivar), [filtersize_l, 1, 1])
    rms_map = 1. / np.mean(snrcube, axis=0)

    if return_rms_map:
        return snrcube, rms_map
    else:
        return snrcube

class FakeData(object):
    '''
    Make a fake IFU and fake DAP stuff
    '''
    def __init__(self, lam_model, spec_model, meta_model,
                 row, drp_base, dap_base, plateifu_base, model_ix,
                 Kspec_obs=None, sky=None):
        '''
        create mocks of DRP LOGCUBE and DAP MAPS

        1. characterize SNR of observed data
        2. redshift model to observed-frame
        3. attenuate according to MW reddening law
        4. blur according to instrumental dispersion
        5. resample onto rectified (observed) wavelength grid
        6. add noise from full covariance prescription OR from SNR
        7. scale according to r-band surface brightness
        8. mask where there's no flux
        '''

        self.model_ix = model_ix

        flux_obs = drp_base['FLUX'].data
        ivar_obs = drp_base['IVAR'].data
        lam_obs = drp_base['WAVE'].data
        specres_obs = drp_base['SPECRES'].data

        cubeshape = drp_base['FLUX'].data.shape
        nl_obs, *mapshape = cubeshape
        mapshape = tuple(mapshape)

        '''STEP 1'''
        # find SNR of each pixel in cube (used to scale noise later)
        snrcube_obs, rmsmap_obs = compute_snrcube(
            flux=flux_obs, ivar=ivar_obs,
            filtersize_l=15, return_rms_map=True)

        '''STEP 2'''
        # compute the redshift map
        z_cosm = row['nsa_zdist']
        z_pec = (dap_base['STELLAR_VEL'].data * u.Unit('km/s') / c.c).to('').value
        z_obs = (1. + z_cosm) * (1. + z_pec) - 1.

        # create a placeholder model cube since flexible broadcasting is hard
        if specmodel.ndim == 3:
            spec_model_cube = specmodel
        else:
            spec_model_cube = np.tile(spec_model[:, None, None], (1, ) + mapshape)
        ivar_model_cube = np.ones_like(spec_model_cube)
        lam_model_z, spec_model_z, ivar_model_z = ut.redshift(
            l=lam_model, f=spec_model_cube, ivar=ivar_model_cube,
            z_in=0., z_out=z_obs)

        '''STEP 3'''
        # figure out attenuation
        # there are issues with extinction library's handling of multidim arrays
        # so we'll interpolate
        atten_l = np.linspace(3000., 20000., 10000)
        r_v = 3.1
        ext_mag_interp = interp1d(
            x=atten_l,
            y=extinction.fitzpatrick99(
                atten_l, r_v=r_v, a_v=drp_base[0].header['EBVGAL'] * r_v),
            fill_value='extrapolate', bounds_error=False)
        ext_mag = ext_mag_interp(lam_model_z)
        atten_factor = 2.5**-ext_mag
        spec_model_mwred = spec_model_z * atten_factor
        ivar_model_mwred = ivar_model_z / atten_factor**2.

        '''STEP 4'''
        # specres of observed cube at model wavelengths
        spec_model_instblur = ut.blur_cube_to_psf(
            l_ref=drp_base['WAVE'].data, specres_ref=drp_base['SPECRES'].data,
            l_eval=lam_model_z, spec_unblurred=spec_model_mwred)

        ivar_model_instblur = ivar_model_mwred

        '''STEP 5'''
        # create placeholder arrays for ivar and flux
        final_fluxcube = np.empty(cubeshape)

        # wavelength grid for final cube
        l_grid = drp_base['WAVE'].data

        # populate flam and ivar pixel-by-pixel
        for ind in np.ndindex(mapshape):
            final_fluxcube[:, ind[0], ind[1]] = np.interp(
                xp=lam_model_z[:, ind[0], ind[1]],
                fp=spec_model_instblur[:, ind[0], ind[1]],
                x=l_grid)
        # normalize each spectrum to mean 1
        final_fluxcube /= np.mean(final_fluxcube, axis=0, keepdims=True)

        '''STEP 6'''
        # spectrophotometric noise
        cov_noise = noisify_cov(Kspec_obs, mapshape=mapshape)
        # random noise: signal * (gauss / snr)
        random_noise = np.random.randn(*cubeshape) / (snrcube_obs + 1.0e-6)
        fluxscaled_random_noise = random_noise * final_fluxcube

        final_fluxcube += (cov_noise + fluxscaled_random_noise)

        '''STEP 7'''
        # normalize everything to have the same observed-frame r-band flux
        u_flam = 1.0e-17 * (u.erg / (u.s * u.cm**2 * u.AA))
        rband_drp = Spec2Phot(
            lam=drp_base['WAVE'].data,
            flam=drp_base['FLUX'].data * u_flam).ABmags['sdss2010-r'] * u.ABmag
        rband_drp[~np.isfinite(rband_drp)] = rband_drp[np.isfinite(rband_drp)].max()
        rband_model = Spec2Phot(
            lam=drp_base['WAVE'].data,
            flam=final_fluxcube * u_flam).ABmags['sdss2010-r'] * u.ABmag
        rband_model[~np.isfinite(rband_model)] = rband_model[np.isfinite(rband_model)].max()
        # flux ratio map
        r = (rband_drp.to(m.Mgy) / rband_model.to(m.Mgy)).value

        final_fluxcube *= r[None, ...]
        # initialize the ivar cube according to the SNR cube
        # of base observations
        # this is because while we think we know the actual spectral covariance,
        # that is not necessarily reflected in the quoted ivars!!!
        final_ivarcube = (snrcube_obs / final_fluxcube)**2.

        # add sky spectrum
        if sky:
            skyfluxs, skyivars = sky.make_skycube(mapshape)
            final_fluxcube = final_fluxcube + skyfluxs
            #final_ivarcube = 1. / (1. / final_ivarcube + 1. / skyivars)


        '''STEP 8'''
        # mask where the native datacube has no signal
        rimg = drp_base['RIMG'].data
        nosignal = (rimg == 0.)[None, ...]
        nosignal_cube = np.broadcast_to(nosignal, final_fluxcube.shape)
        final_fluxcube[nosignal_cube] = 0.
        final_ivarcube[final_fluxcube == 0.] = 0.

        # mask where there's bad velocity info
        badvel = m.mask_from_maskbits(
            dap_base['STELLAR_VEL_MASK'].data, [30])[None, ...]
        final_ivarcube[np.tile(badvel, (nl_obs, 1, 1))] = 0.

        # replace infinite flux elements with median-filtered
        flux_is_inf = ~np.isfinite(final_fluxcube)
        final_fluxcube[flux_is_inf] = medfilt(
            np.nan_to_num(final_fluxcube), [11, 1, 1])[flux_is_inf]

        self.dap_base = dap_base
        self.drp_base = drp_base
        self.fluxcube = final_fluxcube
        self.fluxcube_ivar = final_ivarcube
        self.row = row
        self.metadata = meta_model

        self.plateifu_base = plateifu_base

    @classmethod
    def from_FSPS(cls, fname, i, plateifu_base, pca, row, K_obs,
                  mpl_v, kind, sky=None):

        # load models
        models_hdulist = fits.open(fname)
        models_specs = models_hdulist['flam'].data
        models_lam = models_hdulist['lam'].data

        # restrict wavelength range to same as PCA
        lmin, lmax = pca.l.value.min() - 50., pca.l.value.max() + 50.
        goodlam = (models_lam >= lmin) * (models_lam <= lmax)
        models_lam, models_specs = models_lam[goodlam], models_specs[:, goodlam]

        models_logl = np.log10(models_lam)

        models_meta = t.Table(models_hdulist['meta'].data)
        models_meta['tau_V mu'] = models_meta['tau_V'] * models_meta['mu']
        models_meta['tau_V (1 - mu)'] = models_meta['tau_V'] * (1. - models_meta['mu'])

        models_meta.keep_columns(pca.metadata.colnames)

        for n in models_meta.colnames:
            if pca.metadata[n].meta.get('scale', 'linear') == 'log':
                models_meta[n] = np.log10(models_meta[n])

        # choose specific model
        if i is None:
            # pick at random
            i = np.random.choice(len(models_meta))

        model_spec = models_specs[i, :]
        model_spec /= np.median(model_spec)
        model_meta = models_meta[i]

        # load data
        plate, ifu, *newparams = tuple(plateifu_base.split('-'))

        drp_base = m.load_drp_logcube(plate, ifu, mpl_v)
        dap_base = m.load_dap_maps(plate, ifu, mpl_v, kind)

        return cls(lam_model=models_lam, spec_model=model_spec,
                   meta_model=model_meta, row=row, plateifu_base=plateifu_base,
                   drp_base=drp_base, dap_base=dap_base, model_ix=i,
                   Kspec_obs=K_obs, sky=None)

    @classmethod
    def from_pca_fit(cls, pca_res, row, K_obs):

        spec_model = pca_res.pca.reconstruct_full(pca_res.A)
        drp_base = m.load_drp_logcube(plate, ifu, mpl_v)
        dap_base = m.load_dap_maps(plate, ifu, mpl_v, kind)

        return cls(lam_model=pca_res.l, spec_model=spec_model, 
                   meta_model=[], row=row, plateifu_base=row['plateifu'],
                   drp_base=drp_base, dap_base=dap_base, model_ix='None',
                   Kspec_obs=K_obs, sky=None)

    def resample_spaxel(self, logl_in, flam_in, logl_out):
        '''
        resample the given spectrum to the specified logl grid
        '''

        interp = interp1d(x=logl_in, y=flam_in, kind='linear', bounds_error=False,
                          fill_value=0.)
        # 0. is a sentinel value, that tells us where to mask later
        return interp(logl_out)

    def write(self, fake_basedir):
        '''
        write out fake LOGCUBE and DAP
        '''

        fname_base = self.plateifu_base
        basedir = 'fakedata'
        drp_fname = os.path.join(fake_basedir, '{}_drp.fits'.format(fname_base))
        dap_fname = os.path.join(fake_basedir, '{}_dap.fits'.format(fname_base))
        truthtable_fname = os.path.join(
            fake_basedir, '{}_truth.tab'.format(fname_base))

        new_drp_cube = fits.HDUList([hdu for hdu in self.drp_base])
        new_drp_cube['FLUX'].data = self.fluxcube

        new_drp_cube['IVAR'].data = self.fluxcube_ivar

        new_dap_cube = fits.HDUList([hdu for hdu in self.dap_base])
        sig_a = np.sqrt(
            self.metadata['sigma']**2. * \
                np.ones_like(new_dap_cube['STELLAR_SIGMA'].data) + \
            new_dap_cube['STELLAR_SIGMACORR'].data**2.)
        new_dap_cube['STELLAR_SIGMA'].data = sig_a

        new_drp_cube[0].header['MODEL0'] = self.model_ix
        new_dap_cube[0].header['MODEL0'] = self.model_ix

        truth_tab = t.Table(
            rows=[self.metadata],
            names=self.metadata.colnames)
        truth_tab.write(truthtable_fname, overwrite=True, format='ascii')
        new_drp_cube.writeto(drp_fname, overwrite=True)
        new_dap_cube.writeto(dap_fname, overwrite=True)


def get_stellar_indices(l, spec):
    inds = t.Table(
        data=[t.Column(indices.StellarIndex(n)(l=l, flam=spec), n)
              for n in indices.data['ixname']])

    return inds

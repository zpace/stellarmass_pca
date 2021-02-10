import os, sys, matplotlib
import faulthandler; faulthandler.enable()

mpl_v = 'MPL-11'
daptype = 'SPX-MILESHC-MASTARSSP'
csp_basedir = os.environ['STELLARMASS_PCA_CSPBASE']
manga_results_basedir = os.environ['STELLARMASS_PCA_RESULTSDIR']
mocks_results_basedir = os.path.join(
    os.environ['STELLARMASS_PCA_RESULTSDIR'], 'mocks')

from astropy.cosmology import WMAP9
cosmo = WMAP9

matplotlib.rcParams['font.family'] = 'serif'
matplotlib.rcParams['text.usetex'] = True
if 'DISPLAY' not in os.environ:
    matplotlib.use('agg')

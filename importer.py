import os, sys, matplotlib
import faulthandler; faulthandler.enable()

mpl_v = 'MPL-11' # change with every MPL upgrade
daptype = 'SPX-MILESHC-MASTARSSP' # change with every DAP type code
software_version = '1.1.0' # change with every software version upgrade
csp_basedir = os.environ['STELLARMASS_PCA_CSPBASE']
manga_results_basedir = os.path.join(
    os.environ['STELLARMASS_PCA_RESULTSDIR'], software_version)
mocks_results_basedir = os.path.join(
    os.environ['STELLARMASS_PCA_RESULTSDIR'], software_version, 'mocks')

from astropy.cosmology import WMAP9
cosmo = WMAP9

matplotlib.rcParams['font.family'] = 'serif'
matplotlib.rcParams['text.usetex'] = True
if 'DISPLAY' not in os.environ:
    matplotlib.use('agg')

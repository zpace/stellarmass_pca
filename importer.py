import os, sys, matplotlib
#import faulthandler; faulthandler.enable()

mpl_v = 'MPL-11' # change with every MPL upgrade
daptype = 'SPX-MILESHC-MASTARSSP' # change with every DAP type code
pcay_ver = os.environ['PCAY_VER']
csp_basedir = os.environ['PCAY_CSPBASE']
manga_results_basedir = os.environ['PCAY_RESULTSDIR']
mocks_results_basedir = os.path.join(
    os.environ['PCAY_RESULTSDIR'], 'mocks')

from astropy.cosmology import WMAP9
cosmo = WMAP9

matplotlib.rcParams['font.family'] = 'serif'
matplotlib.rcParams['text.usetex'] = True
if 'DISPLAY' not in os.environ:
    matplotlib.use('agg')

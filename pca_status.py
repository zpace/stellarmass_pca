from importer import *

import os
import numpy as np

def gen_logfile_name(plateifu):
    plate, ifu = plateifu.split('-')
    status_file_dir = os.path.join(
        os.environ['PCAY_RESULTSDIR'], plate)
    status_file = os.path.join(status_file_dir, '{}.log'.format(plateifu))
    
    return status_file

def log_file_exists(plateifu):
    status_file = gen_logfile_name(plateifu)

    return os.path.exists(status_file)

def write_log_file(plateifu, msg):
    '''write a log file
    '''
    status_file = gen_logfile_name(plateifu)
    status_file_dir = os.path.dirname(status_file)
    if not os.path.exists(status_file_dir):
        os.makedirs(status_file_dir)

    with open(os.path.join(status_file, 'w') as logf:
        logf.write(msg)

def summary_remaining(drpall, group_col='ifudesignsize'): 
    remaining = drpall[ 
        ~np.array(list(map(log_file_exists, drpall['plateifu'])))] 
    ifusize_grps = remaining.group_by(group_col) 
    print('remaining galaxies by {}'.format(group_col)) 
    for k, g in zip(ifusize_grps.groups.keys, ifusize_grps.groups): 
        print(k[group_col], ':', len(g))

if __name__ == '__main__':
    import manga_tools as m

    drpall = m.load_drpall(mpl_v)
    drpall = drpall[(drpall['ifudesignsize'] > 0) * (drpall['nsa_z'] != -9999.)]
    print(drpall)
    summary_remaining(drpall)

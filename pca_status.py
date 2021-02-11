from importer import *

import os
import numpy as np
from datetime import datetime as dt
import re

def gen_logfile_name(plateifu):
    plate, ifu = plateifu.split('-')
    status_file_dir = os.path.join(
        os.environ['PCAY_RESULTSDIR'], plate)
    status_file = os.path.join(status_file_dir, '{}.log'.format(plateifu))
    
    return status_file

def log_file_exists(plateifu):
    status_file = gen_logfile_name(plateifu)

    return os.path.exists(status_file)

def log_indicates_complete(plateifu):
    '''judges galaxy completeness and rerun priority based on log-file
    '''
    status_file = gen_logfile_name(plateifu)
    
    if not log_file_exists(plateifu):
        # if log-file hasn't been written, a previous parent process probably died
        # before getting to it, so probably good to re-run
        complete, hipri = False, True
    else:
        # if log file exists, check contents of last two lines for graceful exit and analysis success
        with open(status_file, 'r') as logf:
            lines = logf.readlines()
            if not re.search('ENDING GRACEFULLY', lines[-1]):
                # if last line of logfile does not indicate graceful exit from analysis
                # this is probably a segfault case, so do not prioritize for re-run
                complete, hipri = False, False
            elif not re.search('SUCCESS', lines[-2]):
                # if second-last line of logfile does not indicate success
                # this is probably some other error like missing data,
                # and it would be worth trying to re-run
                complete, hipri = False, True
            else:
                # if last line indicates graceful exit, AND second-last line indicates overall success
                # this galaxy is done, and shouldn't be re-run at all
                complete, hipri = True, False
    return complete, hipri

def write_log_file(plateifu, msg, clobber=False):
    '''write a log file
    '''
    status_file = gen_logfile_name(plateifu)
    status_file_dir = os.path.dirname(status_file)
    if not os.path.exists(status_file_dir):
        os.makedirs(status_file_dir)
    
    msg_withtime = '{} {}'.format(dt.now().strftime('%Y/%m/%d@%H:%M:%S'), msg)

    if clobber:
        mode = 'w'
        msg_logged = msg_withtime
    else:
        mode = 'a'
        msg_logged = '\n{}'.format(msg_withtime)
        

    with open(status_file, mode) as logf:
        logf.write(msg_logged)

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

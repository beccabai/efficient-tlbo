import os
NUM_THREADS = "1"
os.environ["OMP_NUM_THREADS"] = NUM_THREADS         # export OMP_NUM_THREADS=1
os.environ["OPENBLAS_NUM_THREADS"] = NUM_THREADS    # export OPENBLAS_NUM_THREADS=1
os.environ["MKL_NUM_THREADS"] = NUM_THREADS         # export MKL_NUM_THREADS=1
os.environ["VECLIB_MAXIMUM_THREADS"] = NUM_THREADS  # export VECLIB_MAXIMUM_THREADS=1
os.environ["NUMEXPR_NUM_THREADS"] = NUM_THREADS     # export NUMEXPR_NUM_THREADS=1

import re
import sys
import time
import pickle
import argparse
import numpy as np
from functools import partial
from tqdm import tqdm, trange

sys.path.insert(0, '.')
from tlbo.facade.notl import NoTL
from tlbo.facade.rgpe import RGPE
from tlbo.facade.obtl_es import ES
from tlbo.facade.obtl import OBTL
from tlbo.facade.tst import TST
from tlbo.facade.tstm import TSTM
from tlbo.facade.pogpe import POGPE
from tlbo.facade.stacking_gpr import SGPR
from tlbo.facade.scot import SCoT
from tlbo.facade.mklgp import MKLGP
from tlbo.facade.topo_variant1 import OBTLV
from tlbo.facade.topo_variant2 import TOPO
from tlbo.facade.topo_variant3 import TOPO_V3
from tlbo.facade.topo import TransBO_RGPE
from tlbo.facade.mfes import MFES
from tlbo.facade.norm import NORM
from tlbo.facade.norm_minus import NORMMinus
from tlbo.facade.random_surrogate import RandomSearch
from tlbo.framework.smbo_offline import SMBO_OFFLINE
from tlbo.framework.smbo_sst import SMBO_SEARCH_SPACE_TRANSFER
from tlbo.framework.smbo_baseline import SMBO_SEARCH_SPACE_Enlarge
from tlbo.config_space.space_instance import get_configspace_instance

from tools.utils import seeds

parser = argparse.ArgumentParser()
parser.add_argument('--task_id', type=str, default='main')
parser.add_argument('--exp_id', type=str, default='main')
parser.add_argument('--algo_id', type=str, default='random_forest')
parser.add_argument('--methods', type=str, default='rgpe')
parser.add_argument('--surrogate_type', type=str, default='gp')
parser.add_argument('--test_mode', type=str, default='random')
parser.add_argument('--trial_num', type=int, default=50)
parser.add_argument('--init_num', type=int, default=0)
parser.add_argument('--run_num', type=int, default=-1)
parser.add_argument('--num_source_data', type=int, default=50)
parser.add_argument('--num_source_problem', type=int, default=-1)
parser.add_argument('--task_set', type=str, default='class1', choices=['class1', 'class2', 'full'])
parser.add_argument('--num_target_data', type=int, default=10000)
parser.add_argument('--num_random_data', type=int, default=20000)
parser.add_argument('--save_weight', type=str, default='false')
parser.add_argument('--rep', type=int, default=1)
parser.add_argument('--start_id', type=int, default=0)

parser.add_argument('--pmin', type=int, default=10)
parser.add_argument('--pmax', type=int, default=60)
args = parser.parse_args()

algo_id = args.algo_id
exp_id = args.exp_id
task_id = args.task_id
task_set = args.task_set
surrogate_type = args.surrogate_type
n_src_data = args.num_source_data
num_source_problem = args.num_source_problem
n_target_data = args.num_target_data
num_random_data = args.num_random_data
trial_num = args.trial_num
init_num = args.init_num
run_num = args.run_num
test_mode = args.test_mode
save_weight = args.save_weight
baselines = args.methods.split(',')
rep = args.rep
start_id = args.start_id

pmin = args.pmin
pmax = args.pmax

data_dir = 'data/hpo_data/'
assert test_mode in ['bo', 'random']
if init_num > 0:
    enable_init_design = True
else:
    enable_init_design = False
    # Default number of random configurations.
    init_num = 3

algorithms = ['lightgbm', 'random_forest', 'linear', 'adaboost', 'lda', 'extra_trees']
algo_str = '|'.join(algorithms)
pattern = '(.*)-(%s)-(\d+).pkl' % algo_str


def load_hpo_history():
    source_hpo_ids, source_hpo_data = list(), list()
    random_hpo_data = list()
    for _file in tqdm(sorted(os.listdir(data_dir))):
        if _file.endswith('.pkl') and _file.find(algo_id) != -1:
            result = re.search(pattern, _file, re.I)
            if result is None:
                continue
            dataset_id, algo_name, total_trial_num = result.group(1), result.group(2), result.group(3)
            if int(total_trial_num) != n_target_data:
                continue
            with open(data_dir + _file, 'rb') as f:
                data = pickle.load(f)
                perfs = np.array(list(data.values()))
            p_max, p_min = np.max(perfs), np.min(perfs)
            if p_max == p_min:
                continue
            if (perfs == perfs[0]).all():
                continue
            if test_mode == 'random':
                _file = data_dir + '%s-%s-random-%d.pkl' % (dataset_id, algo_id, num_random_data)
                if not os.path.exists(_file):
                    continue
            source_hpo_ids.append(dataset_id)
            source_hpo_data.append(data)
    assert len(source_hpo_ids) == len(source_hpo_data)
    print('Load %s source hpo problems for algorithm %s.' % (len(source_hpo_ids), algo_id))

    # Load random hpo data to test the transfer performance.
    if test_mode == 'random':
        for id, hpo_id in tqdm(list(enumerate(source_hpo_ids))):
            _file = data_dir + '%s-%s-random-%d.pkl' % (hpo_id, algo_id, num_random_data)
            with open(_file, 'rb') as f:
                data = pickle.load(f)
                perfs = np.array(list(data.values()))
                p_max, p_min = np.max(perfs), np.min(perfs)
                if p_max == p_min:
                    print('The same perfs found in the %d-th problem' % id)
                    data = source_hpo_data[id].copy()
                random_hpo_data.append(data)

    print('Load meta-features for each dataset.')
    meta_features = list()
    with open(data_dir + 'dataset_metafeatures.pkl', 'rb') as f:
        dataset_info = pickle.load(f)
        dataset_ids = [item for item in dataset_info['task_ids']]
        dataset_meta_features = list(dataset_info['dataset_embedding'])
        meta_features_dict = dict(zip(dataset_ids, dataset_meta_features))
    for hpo_id in source_hpo_ids:
        assert hpo_id in dataset_ids
        meta_features.append(np.array(meta_features_dict[hpo_id], dtype=np.float64))
    return source_hpo_ids, source_hpo_data, random_hpo_data, meta_features


def extract_data(task_set):
    if task_set == 'full':
        hpo_ids, hpo_data, random_test_data, meta_features = load_hpo_history()
    elif task_set in ['class1', 'class2']:
        if task_set == 'class1':
            dataset_ids = ['kc1', 'pollen', 'madelon', 'winequality_white', 'sick']
        else:
            dataset_ids = ['kc1', 'pollen', 'madelon', 'winequality_white', 'sick', 'quake',
                           'hypothyroid(1)', 'musk', 'page-blocks(1)', 'page-blocks(2)',
                           'satimage', 'segment', 'waveform-5000(2)']

        hpo_ids, hpo_data, random_test_data, meta_features = list(), list(), list(), list()
        hpo_ids_, hpo_data_, random_test_data_, meta_features_ = load_hpo_history()
        for _idx, _id in enumerate(hpo_ids_):
            if _id in dataset_ids:
                hpo_ids.append(hpo_ids_[_idx])
                hpo_data.append(hpo_data_[_idx])
                random_test_data.append(random_test_data_[_idx])
                meta_features.append(meta_features_[_idx])
    else:
        raise ValueError('Invalid Task Set.')
    return hpo_ids, hpo_data, random_test_data, meta_features


if __name__ == "__main__":
    hpo_ids, hpo_data, random_test_data, meta_features = extract_data(task_set)
    algo_name = 'liblinear_svc' if algo_id == 'linear' else algo_id
    config_space = get_configspace_instance(algo_id=algo_name)
    run_num = len(hpo_ids) if run_num == -1 else run_num
    num_source_problem = (len(hpo_ids) - 1) if num_source_problem == -1 else num_source_problem
    # if 'rs' in baselines and len(random_test_data) == 0:
    #     raise ValueError('The random test data is empty!')

    # Exp folder to save results.
    exp_dir = 'data/exp_results/%s_%s_%d_%d/' % (exp_id, test_mode, num_source_problem, num_random_data)
    if not os.path.exists(exp_dir):
        os.makedirs(exp_dir)

    pbar = tqdm(total=rep * len(baselines) * run_num * trial_num)
    for rep_id in range(start_id, start_id + rep):
        for mth in baselines:
            seed = seeds[rep_id]
            print('=== start rep', rep_id, 'seed', seed)
            exp_results = list()
            target_weights = list()
            for id in range(run_num):
                print('=' * 20)
                print('[%s-%s] Evaluate %d-th problem - %s[%d].' % (algo_id, mth, id + 1, hpo_ids[id], rep_id))
                pbar.set_description('[%s-%s] %d-th - %s[%d]' % (algo_id, mth, id + 1, hpo_ids[id], rep_id))
                start_time = time.time()

                # Generate the source and target hpo data.
                source_hpo_data, dataset_meta_features = list(), list()
                if test_mode == 'bo':
                    target_hpo_data = hpo_data[id]
                else:
                    target_hpo_data = random_test_data[id]
                for _id, data in enumerate(hpo_data):
                    if _id != id:
                        source_hpo_data.append(data)
                        dataset_meta_features.append(meta_features[_id])

                # Select a subset of source problems to transfer.
                rng = np.random.RandomState(seed)
                shuffled_ids = np.arange(len(source_hpo_data))
                rng.shuffle(shuffled_ids)
                source_hpo_data = [source_hpo_data[id] for id in shuffled_ids[:num_source_problem]]
                dataset_meta_features = [dataset_meta_features[id] for id in shuffled_ids[:num_source_problem]]
                # Add the meta-features in the target problem.
                dataset_meta_features.append(meta_features[id])

                if mth == 'rgpe':
                    surrogate_class = RGPE
                elif mth == 'notl':
                    surrogate_class = NoTL
                elif mth == 'es':
                    surrogate_class = ES
                elif mth == 'obtl':
                    surrogate_class = OBTL
                elif mth == 'obtlv':
                    surrogate_class = OBTLV
                elif mth == 'tst':
                    surrogate_class = TST
                elif mth == 'pogpe':
                    surrogate_class = POGPE
                elif mth == 'sgpr':
                    surrogate_class = SGPR
                elif mth == 'scot':
                    surrogate_class = SCoT
                elif mth == 'mklgp':
                    surrogate_class = MKLGP
                elif mth == 'rs':
                    surrogate_class = RandomSearch
                elif mth == 'tstm':
                    surrogate_class = TSTM
                elif mth == 'topo':
                    surrogate_class = OBTLV
                elif mth == 'topo_v3':
                    surrogate_class = TOPO_V3
                elif mth == 'ultra':
                    surrogate_class = RGPE
                elif mth in ['space', 'space-all', 'space-sample']:
                    surrogate_class = NORM
                elif mth in ['space-', 'space-all-', 'space-sample-', 'space-_v2']:
                    surrogate_class = NORMMinus

                else:
                    raise ValueError('Invalid baseline name - %s.' % mth)
                if mth not in ['mklgp', 'scot', 'tstm']:
                    surrogate = surrogate_class(config_space, source_hpo_data, target_hpo_data, seed,
                                                surrogate_type=surrogate_type,
                                                num_src_hpo_trial=n_src_data)
                else:
                    surrogate = surrogate_class(config_space, source_hpo_data, target_hpo_data, seed,
                                                surrogate_type=surrogate_type,
                                                num_src_hpo_trial=n_src_data, metafeatures=dataset_meta_features)
                if mth == "ultra":
                    smbo_framework = SMBO_SEARCH_SPACE_TRANSFER
                if mth in ["space", 'space-', 'space-_v2']:
                    smbo_framework = partial(SMBO_SEARCH_SPACE_Enlarge, mode='best')
                elif mth in ['space-all', 'space-all-']:
                    smbo_framework = partial(SMBO_SEARCH_SPACE_Enlarge, mode='all')
                elif mth in ['space-sample', 'space-sample-']:
                    smbo_framework = partial(SMBO_SEARCH_SPACE_Enlarge, mode='sample')
                elif mth == 'box':
                    smbo_framework = partial(SMBO_SEARCH_SPACE_Enlarge, mode='box')
                elif mth == 'ellipsoid':
                    smbo_framework = partial(SMBO_SEARCH_SPACE_Enlarge, mode='ellipsoid')
                else:
                    smbo_framework = SMBO_OFFLINE

                smbo = smbo_framework(target_hpo_data, config_space, surrogate,
                                      random_seed=seed, max_runs=trial_num,
                                      source_hpo_data=source_hpo_data,
                                      num_src_hpo_trial=n_src_data,
                                      surrogate_type=surrogate_type,
                                      enable_init_design=enable_init_design,
                                      initial_runs=init_num,
                                      acq_func='ei')

                if mth.startswith('space'):
                    smbo.p_min = pmin
                    smbo.p_max = pmax
                    print('use pmin/max:', smbo.p_min, smbo.p_max)
                if mth in ['space-_v2']:
                    smbo.use_correct_rate = True

                result = list()
                rnd_target_perfs = [_perf for (_, _perf) in list(random_test_data[id].items())]
                rnd_ymax, rnd_ymin = np.max(rnd_target_perfs), np.min(rnd_target_perfs)

                for _iter_id in range(trial_num):
                    if surrogate.method_id == 'rs':
                        _perfs = rnd_target_perfs[:(_iter_id + 1)]
                        y_inc = np.min(_perfs)
                        adtm = (y_inc - rnd_ymin) / (rnd_ymax - rnd_ymin)
                        result.append([adtm, y_inc, 0.1])
                    else:
                        config, _, perf, _ = smbo.iterate()
                        time_taken = time.time() - start_time
                        adtm, y_inc = smbo.get_adtm(), smbo.get_inc_y()
                        result.append([adtm, y_inc, time_taken])
                    pbar.update(1)
                exp_results.append(result)
                print('In %d-th problem: %s' % (id, hpo_ids[id]), 'adtm, y_inc', result[-1])
                print('min/max', smbo.y_min, smbo.y_max)
                print('mean,std', np.mean(smbo.ys), np.std(smbo.ys))
                if hasattr(surrogate, 'hist_ws'):
                    weights = np.array(surrogate.hist_ws)
                    trans = lambda x: ','.join([('%.2f' % item) for item in x])
                    weight_str = '\n'.join([trans(item) for item in weights])
                    print(weight_str)
                    print('Weight stats.')
                    print(trans(np.mean(weights, axis=0)))
                    source_ids = [item[0] for item in enumerate(list(np.mean(weights, axis=0))) if item[1] >= 1e-2]
                    print('Source problems used', source_ids)

                target_weights.append(surrogate.target_weight)

                # Save the running results on the fly with overwriting.
                if run_num == len(hpo_ids):
                    if mth.startswith('space'):
                        mth_file = '%s_%d_%d_%s_%d_%d_%s_%s_%d.pkl' % (
                            mth, pmin, pmax, algo_id, n_src_data, trial_num, surrogate_type, task_id, seed)
                    else:
                        mth_file = '%s_%s_%d_%d_%s_%s_%d.pkl' % (
                            mth, algo_id, n_src_data, trial_num, surrogate_type, task_id, seed)
                    with open(exp_dir + mth_file, 'wb') as f:
                        data = [np.array(exp_results), np.mean(exp_results, axis=0)]
                        pickle.dump(data, f)

                    if save_weight == 'true':
                        mth_file = 'w_%s_%s_%d_%d_%s_%s_%d.pkl' % (
                            mth, algo_id, n_src_data, trial_num, surrogate_type, task_id, seed)
                        with open(exp_dir + mth_file, 'wb') as f:
                            data = target_weights
                            pickle.dump(data, f)
    pbar.close()

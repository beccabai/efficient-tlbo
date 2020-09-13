import os
import sys
import time
import pickle
import argparse
import numpy as np

sys.path.append(os.getcwd())
from tlbo.framework.smbo_offline import SMBO_OFFLINE

parser = argparse.ArgumentParser()
parser.add_argument('--algo_id', type=str, default='random_forest')
parser.add_argument('--methods', type=str, default='rgpe')
parser.add_argument('--surrogate_type', type=str, default='gp')
parser.add_argument('--trial_num', type=int, default=50)
parser.add_argument('--num_source_data', type=int, default=50)
args = parser.parse_args()
algo_id = args.algo_id
surrogate_type = args.surrogate_type
n_src_data = args.num_source_data
trial_num = args.trial_num
baselines = args.methods.split(',')
data_dir = 'data/hpo_data/'
exp_dir = 'data/exp_results/'


def load_hpo_history():
    source_hpo_ids = list()
    source_hpo_data = list()
    for _file in os.listdir(data_dir):
        if _file.endswith('.pkl') and _file.find(algo_id) != -1:
            dataset_id = _file.split('-')[0]
            with open(data_dir + _file, 'rb') as f:
                data = pickle.load(f)
                perfs = np.array(list(data.values()))
            if (perfs == perfs[0]).all():
                continue
            source_hpo_ids.append(dataset_id)
            source_hpo_data.append(data)
    assert len(source_hpo_ids) == len(source_hpo_data)
    print('Load %s source hpo problems for algorithm %s.' % (len(source_hpo_ids), algo_id))
    return source_hpo_ids, source_hpo_data


if __name__ == "__main__":
    hpo_ids, hpo_data = load_hpo_history()
    from tlbo.facade.notl import NoTL
    from tlbo.facade.rgpe import RGPE
    from tlbo.facade.ensemble_selection import ES
    from tlbo.facade.random_surrogate import RandomSearch
    from tlbo.config_space.space_instance import get_configspace_instance
    config_space = get_configspace_instance(algo_id=algo_id)
    exp_results = list()

    for mth in baselines:
        for id in range(len(hpo_ids)):
            print('=' * 20)
            print('Start to evaluate %d-th target problem.' % (id + 1))
            start_time = time.time()
            # Generate the source and target hpo data.
            target_hpo_data = hpo_data[id]
            source_hpo_data = list()
            for _id, data in enumerate(hpo_data):
                if _id != id:
                    source_hpo_data.append(data)
            rng = np.random.RandomState(1)
            if mth == 'rgpe':
                surrogate_class = RGPE
            elif mth == 'notl':
                surrogate_class = NoTL
            elif mth == 'es':
                surrogate_class = ES
            elif mth == 'rs':
                surrogate_class = RandomSearch
            else:
                raise ValueError('Invalid baseline name - %s.' % mth)
            surrogate = surrogate_class(config_space, source_hpo_data, target_hpo_data, rng,
                                        surrogate_type=surrogate_type,
                                        num_src_hpo_trial=n_src_data)
            smbo = SMBO_OFFLINE(target_hpo_data, config_space, surrogate, max_runs=trial_num)
            result = list()
            for _ in range(trial_num):
                config, _, perf, _ = smbo.iterate()
                # print(config, perf)
                time_taken = time.time() - start_time
                adtm, y_inc = smbo.get_adtm(), smbo.get_inc_y()
                # print('%.3f - %.3f' % (adtm, y_inc))
                result.append([adtm, y_inc, time_taken])
            exp_results.append(result)
        mth_file = '%s_%s_%d_%d.pkl' % (mth, algo_id, n_src_data, trial_num)
        with open(exp_dir + mth_file, 'wb') as f:
            data = [np.array(exp_results), np.mean(exp_results, axis=0)]
            pickle.dump(data, f)

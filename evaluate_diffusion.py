import argparse
import os

import numpy as np
from rdkit import Chem
from rdkit import RDLogger
import torch
from tqdm.auto import tqdm
from glob import glob
from collections import Counter

from utils.evaluation import eval_atom_type, scoring_func, analyze, eval_bond_length
from utils import misc, reconstruct, transforms
from utils.evaluation.docking_qvina import QVinaDockingTask
from utils.evaluation.docking_vina import VinaDockingTask


def print_dict(d, logger):
    for k, v in d.items():
        if v is not None:
            logger.info(f'{k}:\t{v:.4f}')
        else:
            logger.info(f'{k}:\tNone')


def print_ring_ratio(all_ring_sizes, logger):
    for ring_size in range(3, 10):
        n_mol = 0
        for counter in all_ring_sizes:
            if ring_size in counter:
                n_mol += 1
        logger.info(f'ring size: {ring_size} ratio: {n_mol / len(all_ring_sizes):.3f}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample_path', type=str,default='./')
    parser.add_argument('--verbose', type=eval, default=False)
    parser.add_argument('--eval_step', type=int, default=-1)
    parser.add_argument('--eval_num_examples', type=int, default=None)
    parser.add_argument('--save', type=eval, default=True)
    parser.add_argument('--protein_root', type=str, default='./data/test_set')
    parser.add_argument('--atom_enc_mode', type=str, default='add_aromatic')
    parser.add_argument('--docking_mode', type=str,default='none', choices=['qvina', 'vina_score', 'vina_dock', 'none'])
    parser.add_argument('--exhaustiveness', type=int, default=16)
    args = parser.parse_args()

    result_path = args.sample_path
    logger = misc.get_logger('evaluate', log_dir=result_path)
    r = torch.load('./metrics_-1.pt')
    num_examples = len(r['all_results'])
    logger.info(f'Load generated data done! {num_examples} examples in total.')

    num_samples = num_examples
    all_mol_stable, all_atom_stable, all_n_atom = 0, 0, 0
    n_recon_success, n_eval_success, n_complete = 0, 0, 0
    results = []
    all_pair_dist, all_bond_dist = [], []
    all_atom_types = Counter()
    success_pair_dist, success_atom_types = [], Counter()
     

    for sample_idx, (pred_pos, pred_v) in enumerate(zip(r['all_results'][:], r['all_results'][:])):
        pred_pos, pred_v = pred_pos['pred_pos'], pred_v['pred_v']

        # stability check
        pred_atom_type = transforms.get_atomic_number_from_index(pred_v, mode=args.atom_enc_mode)
        all_atom_types += Counter(pred_atom_type)
        r_stable = analyze.check_stability(pred_pos, pred_atom_type)
        all_mol_stable += r_stable[0]
        all_atom_stable += r_stable[1]
        all_n_atom += r_stable[2]

        pair_dist = eval_bond_length.pair_distance_from_pos_v(pred_pos, pred_atom_type)
        all_pair_dist += pair_dist

        # reconstruction
        try:
            pred_aromatic = transforms.is_aromatic_from_index(pred_v, mode=args.atom_enc_mode)
            mol = reconstruct.reconstruct_from_generated(pred_pos, pred_atom_type, pred_aromatic)
            smiles = Chem.MolToSmiles(mol)
        except reconstruct.MolReconsError:
            if args.verbose:
                logger.warning('Reconstruct failed %s' % f'{example_idx}_{sample_idx}')
            continue
        n_recon_success += 1

        if '.' in smiles:
            continue
        n_complete += 1

        # chemical and docking check
        try:
            chem_results = scoring_func.get_chem(mol)
            if args.docking_mode == 'qvina':
                vina_task = QVinaDockingTask.from_generated_mol(
                    mol, r['data'].ligand_filename, protein_root=args.protein_root)
                vina_results = vina_task.run_sync()
            elif args.docking_mode in ['vina_score', 'vina_dock']:
                vina_task = VinaDockingTask.from_generated_mol(
                    mol, r['data'].ligand_filename, protein_root=args.protein_root)
                score_only_results = vina_task.run(mode='score_only', exhaustiveness=args.exhaustiveness)
                minimize_results = vina_task.run(mode='minimize', exhaustiveness=args.exhaustiveness)
                vina_results = {
                    'score_only': score_only_results,
                    'minimize': minimize_results
                }
                if args.docking_mode == 'vina_dock':
                    docking_results = vina_task.run(mode='dock', exhaustiveness=args.exhaustiveness)
                    vina_results['dock'] = docking_results
            else:
                vina_results = None

            n_eval_success += 1
        except:
            if args.verbose:
                logger.warning('Evaluation failed for %s' % f'{example_idx}_{sample_idx}')
            continue

        # now we only consider complete molecules as success
        bond_dist = eval_bond_length.bond_distance_from_mol(mol)
        all_bond_dist += bond_dist

        success_pair_dist += pair_dist
        success_atom_types += Counter(pred_atom_type)

    logger.info(f'Evaluate done! {num_samples} samples in total.')

    fraction_mol_stable = all_mol_stable / num_samples
    fraction_atm_stable = all_atom_stable / all_n_atom
    fraction_recon = n_recon_success / num_samples
    fraction_eval = n_eval_success / num_samples
    fraction_complete = n_complete / num_samples
    validity_dict = {
        'mol_stable': fraction_mol_stable,
        'atm_stable': fraction_atm_stable,
        'recon_success': fraction_recon,
        'eval_success': fraction_eval,
        'complete': fraction_complete
    }
    print_dict(validity_dict, logger)

    c_bond_length_profile = eval_bond_length.get_bond_length_profile(all_bond_dist)
    c_bond_length_dict = eval_bond_length.eval_bond_length_profile(c_bond_length_profile)
    logger.info('JS bond distances of complete mols: ')
    print_dict(c_bond_length_dict, logger)

    success_pair_length_profile = eval_bond_length.get_pair_length_profile(success_pair_dist)
    success_js_metrics = eval_bond_length.eval_pair_length_profile(success_pair_length_profile)
    print_dict(success_js_metrics, logger)

    atom_type_js = eval_atom_type.eval_atom_type_distribution(success_atom_types)
    logger.info('Atom type JS: %.4f' % atom_type_js)

    if args.save:
        eval_bond_length.plot_distance_hist(success_pair_length_profile,
                                            metrics=success_js_metrics,
                                            save_path=os.path.join(result_path, f'pair_dist_hist_{args.eval_step}.png'))

    logger.info('Number of reconstructed mols: %d, complete mols: %d, evaluated mols: %d' % (
        n_recon_success, n_complete, len(results)))

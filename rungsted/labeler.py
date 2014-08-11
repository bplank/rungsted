# coding: utf-8
import argparse
from collections import defaultdict
import copy
import json
import logging
import os
import cPickle
import IPython
import numpy as np
import sys
from os.path import exists, join
import time

from decoding import Viterbi as ViterbiStd
#from decoding_pd import Viterbi as ViterbiPd
from feat_map import HashingFeatMap, DictFeatMap

from input import read_vw_seq, count_group_sizes, dropout_groups
from timer import Timer
from struct_perceptron import avg_loss, accuracy, update_weights, drop_out
from weights import WeightVector


logging.basicConfig(format='%(asctime)s : %(levelname)s : %(message)s', level=logging.INFO)

parser = argparse.ArgumentParser(description="""Structured perceptron tagger.""")
parser.add_argument('--train', help="Training data (vw format).")
parser.add_argument('--test', help="Test data (vw format).")
parser.add_argument('--hash-bits', '-b', help="Size of feature vector in bits (2**b).", type=int)
parser.add_argument('--passes', help="Number of passes over the training set.", type=int, default=5)
parser.add_argument('--predictions', '-p', help="File for outputting predictions.")
parser.add_argument('--ignore', help="One-character prefix of namespaces to ignore.", nargs='*', default=[])
parser.add_argument('--quadratic', '-q', help="Combine features in these two namespace, identified by a one-character prefix of their name"
                                              "':' is a short-hand for all namespaces.", nargs='*', default=[])
parser.add_argument('--no-average', help="Do not average over all updates.", action='store_false',
                    dest='average', default=True)
parser.add_argument('--no-ada-grad', help="Do not use adaptive gradient scaling.",
                    action='store_false', dest='ada_grad', default=True)
parser.add_argument('--initial-model', '-i', help="Initial model from this file.")
parser.add_argument('--final-model', '-f', help="Save model here after training.")
parser.add_argument('--cost-sensitive', '--cs', help="Cost-sensitive weight updates", action='store_true')
parser.add_argument('--append-test', help="Append test result as JSON object to this file.")
parser.add_argument('--audit', help="Print the interpretation of the input files to standard out. "
                                    "Useful for debugging. ", action='store_true')
parser.add_argument('--name', help="Identify this invocation by NAME (use in conjunction with --append-test).")
parser.add_argument('--labels', help="Read the set of labels from this file.")
parser.add_argument('--drop-out', help="Regularize by randomly removing features (with probability 0.1).", action='store_true')
parser.add_argument('--decoder', '-d', help="Use this decoder to find the best sequence given the constraints",
                    choices=('viterbi', 'viterbi_pd'), default='viterbi')


args = parser.parse_args()

timers = defaultdict(lambda: Timer())
logging.info("Tagger started. \nCalled with {}".format(args))

if args.hash_bits:
    feat_map = HashingFeatMap(args.hash_bits)
else:
    feat_map = DictFeatMap()

weight_updater = update_weights
if args.labels:
    labels = [line.strip() for line in open(args.labels)]
else:
    labels = None

if args.initial_model:
    wt = WeightVector.load(join(args.initial_model, 'transition.npz'))
    we = WeightVector.load(join(args.initial_model, 'emission.npz'))
    labels = list(np.load(join(args.initial_model, 'labels.npy')))
    if not args.hash_bits:
        feat_map.feat2index_ = cPickle.load(open(join(args.initial_model, 'feature_map.pickle')))

train = None
if args.train:
    train, train_labels = read_vw_seq(args.train, ignore=args.ignore, quadratic=args.quadratic, feat_map=feat_map,
                                      labels=labels, audit=args.audit)
    if args.initial_model:
        assert len(labels) == len(train_labels), \
            "Labels from training data not found in saved model".format(set(train_labels) - set(labels))
    labels = train_labels
    logging.info("Training data {} sentences {} labels".format(len(train), len(train_labels)))

# Prevents the addition of new features when loading the test set
feat_map.freeze()
test = None
if args.test:
    test, test_labels = read_vw_seq(args.test, ignore=args.ignore, quadratic=args.quadratic, feat_map=feat_map,
                                    labels=labels, audit=args.audit)
    if args.initial_model:
        assert len(labels) == len(test_labels), \
            "Labels from test data not found in saved model: {}".format(set(test_labels) - set(labels))
    labels = test_labels
    logging.info("Test data {} sentences {} labels".format(len(test), len(test_labels)))

n_labels = len(labels)
if not args.hash_bits:
    feat_map.n_labels = n_labels

# Loading weights
if not args.initial_model:
    wt = WeightVector((n_labels + 2, n_labels + 2), ada_grad=args.ada_grad)
    we = WeightVector(feat_map.n_feats(), ada_grad=args.ada_grad)

logging.info("Weight vector sizes. Transition={}. Emission={}".format(wt.dims, we.dims))

# Counting group sizes
if args.train and args.drop_out:
    logging.info("Counting group sizes")
    group_sizes = count_group_sizes(train)
else:
    group_sizes = None
n_updates = 0

Viterbi = ViterbiStd if args.decoder == 'viterbi' else ViterbiPd



def do_train(transition, emission):
    vit = Viterbi(n_labels, transition, emission, feat_map)

    timers['train'].begin()
    n_updates = 0
    for epoch in range(1, args.passes+1):
        for sent in train:
            vit.decode(sent)
            weight_updater(sent, transition, emission, 0.1, n_labels, feat_map)
            n_updates += 1
            transition.n_updates = n_updates
            emission.n_updates = n_updates

            if n_updates % 1000 == 0:
                print >>sys.stderr, '\r[{}] {}k sentences total'.format(epoch, n_updates / 1000),

        epoch_msg = "[{}] train loss={:.4f} ".format(epoch, avg_loss(train))
        print >>sys.stderr, "\r{}{}".format(epoch_msg, " "*72)

    timers['train'].end()
    if args.average:
        transition.average()
        emission.average()

    tokens_trained = epoch * sum(len(seq) for seq in train)
    print >>sys.stderr, "Training took {:.2f} secs. {} words/sec".format(timers['train'].elapsed(),
                                                                         int(tokens_trained / timers['train'].elapsed()))
def do_test(transition, emission):
    vit = Viterbi(n_labels, transition, emission, feat_map)

    timers['test'].begin()
    with open(args.predictions or os.devnull, 'w') as out:
        labels_map = dict((i, label_str) for i, label_str in enumerate(test_labels))
        for sent_i, sent in enumerate(test):
            if sent_i > 0:
                print >>out, ""
            vit.decode(sent)
            for example_id, gold_label, pred_label in zip(sent.ids, sent.gold_labels, sent.pred_labels):
                print >>out, "{}\t{}\t{}".format(example_id, labels_map[gold_label], labels_map[pred_label])


    timers['test'].end()
    logging.info("Accuracy: {:.4f}".format(accuracy(test)))
    print >>sys.stderr, "Test took {:.2f} secs. {} words/sec".format(timers['test'].elapsed(),
                                                                         int(sum(len(seq) for seq in test) / timers['test'].elapsed()))

    if args.append_test:
        with open(args.append_test, 'a') as result_file:
            result = {'accuracy': accuracy(test), 'name': args.name}
            result.update(args.__dict__)
            json.dump(result, result_file)
            print >>result_file, ""


# Training
if args.train:
    if args.drop_out:
        wt_total = np.zeros_like(wt.w)
        we_total = np.zeros_like(we.w)
        for i in range(25):
            dropout_groups(train, group_sizes)
            wt_round = wt.copy()
            we_round = we.copy()

            do_train(wt_round, we_round)
            we_total += we_round.w
            wt_total += wt_round.w

        wt = WeightVector(wt.dims, w=wt_total)
        we = WeightVector(we.dims, w=we_total)

    else:
        do_train(wt, we)


# Testing
if args.test:
    do_test(wt, we)

# Save model
if args.final_model:
    if not exists(args.final_model):
        os.makedirs(args.final_model)

    wt.save(join(args.final_model, 'transition.npz'))
    we.save(join(args.final_model, 'emission.npz'))
    np.save(join(args.final_model, 'labels'), labels)
    json.dump(args.__dict__, open(join(args.final_model, 'settings.json'), 'w'))

    if not args.hash_bits:
        cPickle.dump(feat_map.feat2index_,
                     open(join(args.final_model, 'feature_map.pickle'), 'w'), protocol=2)


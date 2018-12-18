import numpy as np
from numpy.testing import assert_almost_equal
from numpy.testing import assert_array_almost_equal
import pytest

from pygbm.splitting import _find_histogram_split
from pygbm.splitting import (SplittingContext, find_node_split,
                             find_node_split_subtraction,
                             split_indices_parallel, split_indices_single_thread)


@pytest.mark.parametrize('n_bins', [3, 32, 256])
def test_histogram_split(n_bins):
    rng = np.random.RandomState(42)
    feature_idx = 0
    l2_regularization = 0
    min_hessian_to_split = 1e-3
    min_samples_leaf = 1
    min_gain_to_split = 0.
    X_binned = np.asfortranarray(
        rng.randint(0, n_bins, size=(int(1e4), 2)), dtype=np.uint8)
    binned_feature = X_binned.T[feature_idx]
    sample_indices = np.arange(binned_feature.shape[0], dtype=np.uint32)
    ordered_hessians = np.ones_like(binned_feature, dtype=np.float32)
    all_hessians = ordered_hessians

    for true_bin in range(1, n_bins - 1):
        for sign in [-1, 1]:
            ordered_gradients = np.full_like(binned_feature, sign,
                                             dtype=np.float32)
            ordered_gradients[binned_feature <= true_bin] *= -1
            all_gradients = ordered_gradients

            n_bins_per_feature = np.array([n_bins] * X_binned.shape[1],
                                          dtype=np.uint32)
            context = SplittingContext(X_binned,
                                       n_bins,
                                       n_bins_per_feature,
                                       all_gradients, all_hessians,
                                       l2_regularization,
                                       min_hessian_to_split,
                                       min_samples_leaf, min_gain_to_split,
                                       True)

            split_info, _ = _find_histogram_split(context, feature_idx,
                                                  sample_indices)

            assert split_info.bin_idx == true_bin
            assert split_info.gain >= 0
            assert split_info.feature_idx == feature_idx
            assert (split_info.n_samples_left + split_info.n_samples_right
                    == sample_indices.shape[0])
            # Constant hessian: 1. per sample.
            assert split_info.n_samples_left == split_info.hessian_left


@pytest.mark.parametrize('constant_hessian', [True, False])
def test_split_vs_split_subtraction(constant_hessian):
    # Make sure find_node_split and find_node_split_subtraction return the
    # same results.
    # Should we add a test about computation time to make sure
    # time(subtraction) < time(regular)?
    rng = np.random.RandomState(42)

    n_bins = 10
    n_features = 20
    n_samples = 500
    l2_regularization = 0.
    min_hessian_to_split = 1e-3
    min_samples_leaf = 1
    min_gain_to_split = 0.

    X_binned = rng.randint(0, n_bins, size=(n_samples, n_features),
                           dtype=np.uint8)
    X_binned = np.asfortranarray(X_binned)
    sample_indices = np.arange(n_samples, dtype=np.uint32)
    all_gradients = rng.randn(n_samples).astype(np.float32)
    if constant_hessian:
        all_hessians = np.ones(1, dtype=np.float32)
    else:
        all_hessians = rng.lognormal(size=n_samples).astype(np.float32)

    n_bins_per_feature = np.array([n_bins] * X_binned.shape[1],
                                  dtype=np.uint32)
    context = SplittingContext(X_binned, n_bins,
                               n_bins_per_feature,
                               all_gradients, all_hessians,
                               l2_regularization, min_hessian_to_split,
                               min_samples_leaf, min_gain_to_split, True)

    mask = rng.randint(0, 2, n_samples).astype(np.bool)
    sample_indices_left = sample_indices[mask]
    sample_indices_right = sample_indices[~mask]

    # first split parent, left and right with classical method
    si_parent, hists_parent = find_node_split(context, sample_indices)
    si_left, hists_left = find_node_split(context, sample_indices_left)
    si_right, hists_right = find_node_split(context, sample_indices_right)

    # split left with subtraction method
    si_left_sub, hists_left_sub = find_node_split_subtraction(
        context, sample_indices_left, hists_parent, hists_right)

    # split right with subtraction method
    si_right_sub, hists_right_sub = find_node_split_subtraction(
        context, sample_indices_right, hists_parent, hists_left)

    # make sure histograms from classical and subtraction method are the same
    for hists, hists_sub in ((hists_left, hists_left_sub),
                             (hists_right, hists_right_sub)):
        for hist, hist_sub in zip(hists, hists_sub):
            for key in ('count', 'sum_hessians', 'sum_gradients'):
                assert_array_almost_equal(hist[key], hist_sub[key], decimal=4)

    # make sure split_infos from classical and subtraction method are the same
    for si, si_sub in ((si_left, si_left_sub), (si_right, si_right_sub)):
        assert_almost_equal(si.gain, si_sub.gain, decimal=3)
        assert_almost_equal(si.feature_idx, si_sub.feature_idx, decimal=3)
        assert_almost_equal(si.gradient_left, si_sub.gradient_left, decimal=3)
        assert_almost_equal(si.gradient_right, si_sub.gradient_right,
                            decimal=3)
        assert_almost_equal(si.hessian_right, si_sub.hessian_right, decimal=3)
        assert_almost_equal(si.hessian_left, si_sub.hessian_left, decimal=3)


@pytest.mark.parametrize('constant_hessian', [True, False])
def test_gradient_and_hessian_sanity(constant_hessian):
    # This test checks that the values of gradients and hessians are
    # consistent in different places:
    # - in split_info: si.gradient_left + si.gradient_right must be equal to
    #   the gradient at the node. Same for hessians.
    # - in the histograms: summing 'sum_gradients' over the bins must be
    #   constant across all features, and those sums must be equal to the
    #   node's gradient. Same for hessians.
    #
    # These checks are carried out for split_info and histograms resulting
    # from both find_node_split() and find_node_split_subtraction().
    #
    # The structure of this test is exactly the same as in
    # test_split_vs_split_subtraction() but it's probably best to keep them
    # separate because they're not checking the same things.

    rng = np.random.RandomState(42)

    n_bins = 10
    n_features = 20
    n_samples = 500
    l2_regularization = 0.
    min_hessian_to_split = 1e-3
    min_samples_leaf = 1
    min_gain_to_split = 0.

    X_binned = rng.randint(0, n_bins, size=(n_samples, n_features),
                           dtype=np.uint8)
    X_binned = np.asfortranarray(X_binned)
    sample_indices = np.arange(n_samples, dtype=np.uint32)
    all_gradients = rng.randn(n_samples).astype(np.float32)
    if constant_hessian:
        all_hessians = np.ones(1, dtype=np.float32)
    else:
        all_hessians = rng.lognormal(size=n_samples).astype(np.float32)

    n_bins_per_feature = np.array([n_bins] * X_binned.shape[1],
                                  dtype=np.uint32)
    context = SplittingContext(X_binned, n_bins,
                               n_bins_per_feature,
                               all_gradients, all_hessians,
                               l2_regularization, min_hessian_to_split,
                               min_samples_leaf, min_gain_to_split, True)

    mask = rng.randint(0, 2, n_samples).astype(np.bool)
    sample_indices_left = sample_indices[mask]
    sample_indices_right = sample_indices[~mask]

    # first split parent, left and right with classical method
    si_parent, hists_parent = find_node_split(context, sample_indices)
    si_left, hists_left = find_node_split(context, sample_indices_left)
    si_right, hists_right = find_node_split(context, sample_indices_right)

    # split left with subtraction method
    si_left_sub, hists_left_sub = find_node_split_subtraction(
        context, sample_indices_left, hists_parent, hists_right)

    # split right with subtraction method
    si_right_sub, hists_right_sub = find_node_split_subtraction(
        context, sample_indices_right, hists_parent, hists_left)

    # make sure that si.gradient_left + si.gradient_right have their expected
    # value, same for hessians
    for si, indices in (
            (si_parent, sample_indices),
            (si_left, sample_indices_left),
            (si_left_sub, sample_indices_left),
            (si_right, sample_indices_right),
            (si_right_sub, sample_indices_right)):
        gradient = si.gradient_right + si.gradient_left
        expected_gradient = all_gradients[indices].sum()
        hessian = si.hessian_right + si.hessian_left
        if constant_hessian:
            expected_hessian = indices.shape[0] * all_hessians[0]
        else:
            expected_hessian = all_hessians[indices].sum()

        assert_almost_equal(gradient, expected_gradient, decimal=3)
        assert_almost_equal(hessian, expected_hessian, decimal=3)

    # make sure sum of gradients in histograms are the same for all features,
    # and make sure they're equal to their expected value
    for hists, indices in (
            (hists_parent, sample_indices),
            (hists_left, sample_indices_left),
            (hists_left_sub, sample_indices_left),
            (hists_right, sample_indices_right),
            (hists_right_sub, sample_indices_right)):
        # note: gradients and hessians have shape (n_features,),
        # we're comparing them to *scalars*. This has the benefit of also
        # making sure that all the entries are equal.
        gradients = hists['sum_gradients'].sum(axis=1)  # shape = (n_features,)
        expected_gradient = all_gradients[indices].sum()  # scalar
        hessians = hists['sum_hessians'].sum(axis=1)
        if constant_hessian:
            # 0 is not the actual hessian, but it's not computed in this case
            expected_hessian = 0.
        else:
            expected_hessian = all_hessians[indices].sum()

        assert_almost_equal(gradients, expected_gradient, decimal=4)
        assert_almost_equal(hessians, expected_hessian, decimal=4)


def test_split_indices():
    # Check that split_indices returns the correct splits and that
    # splitting_context.partition is consistent with what is returned.
    rng = np.random.RandomState(421)

    n_bins = 5
    n_samples = 10
    l2_regularization = 0.
    min_hessian_to_split = 1e-3
    min_samples_leaf = 1
    min_gain_to_split = 0.

    # split will happen on feature 1 and on bin 3
    X_binned = [[0, 0],
                [0, 3],
                [0, 4],
                [0, 0],
                [0, 0],
                [0, 0],
                [0, 0],
                [0, 4],
                [0, 0],
                [0, 4]]
    X_binned = np.asfortranarray(X_binned, dtype=np.uint8)
    sample_indices = np.arange(n_samples, dtype=np.uint32)
    all_gradients = rng.randn(n_samples).astype(np.float32)
    all_hessians = np.ones(1, dtype=np.float32)

    n_bins_per_feature = np.array([n_bins] * X_binned.shape[1],
                                  dtype=np.uint32)
    context = SplittingContext(X_binned, n_bins,
                               n_bins_per_feature,
                               all_gradients, all_hessians,
                               l2_regularization, min_hessian_to_split,
                               min_samples_leaf, min_gain_to_split, True)

    assert_array_almost_equal(sample_indices, context.partition)
    si_root, _ = find_node_split(context, sample_indices)

    # sanity checks for best split
    assert si_root.feature_idx == 1
    assert si_root.bin_idx == 3

    samples_left, samples_right = split_indices_parallel(
        context, si_root, context.partition.view())
    assert set(samples_left) == set([0, 1, 3, 4, 5, 6, 8])
    assert set(samples_right) == set([2, 7, 9])

    position_right = len(samples_left)

    assert_array_almost_equal(samples_left,
                              context.partition[:position_right])
    assert_array_almost_equal(samples_right,
                              context.partition[position_right:])

    # Check that the resulting split indices sizes are consistent with the
    # count statistics anticipated when looking for the best split.
    assert samples_left.shape[0] == si_root.n_samples_left
    assert samples_right.shape[0] == si_root.n_samples_right

    samples_left_single_thread, samples_right_single_thread = split_indices_single_thread(
        context, si_root, context.partition.view())

    assert samples_left.tolist() == samples_left_single_thread.tolist()
    assert samples_right.tolist() == samples_right_single_thread.tolist()


def test_min_gain_to_split():
    # Try to split a pure node (all gradients are equal, same for hessians)
    # with min_gain_to_split = 0 and make sure that the node is not split (best
    # possible gain = -1). Note: before the strict inequality comparison, this
    # test would fail because the node would be split with a gain of 0.
    rng = np.random.RandomState(42)
    feature_idx = 0
    l2_regularization = 0
    min_hessian_to_split = 0
    min_samples_leaf = 1
    min_gain_to_split = 0.
    n_bins = 255
    n_samples = 100
    X_binned = np.asfortranarray(
        rng.randint(0, n_bins, size=(n_samples, 2)), dtype=np.uint8)
    binned_feature = X_binned.T[feature_idx]
    sample_indices = np.arange(n_samples, dtype=np.uint32)
    all_hessians = np.ones_like(binned_feature, dtype=np.float32)
    all_gradients = np.ones_like(binned_feature, dtype=np.float32)

    n_bins_per_feature = np.array([n_bins] * X_binned.shape[1],
                                  dtype=np.uint32)
    context = SplittingContext(X_binned, n_bins, n_bins_per_feature,
                               all_gradients, all_hessians,
                               l2_regularization,
                               min_hessian_to_split,
                               min_samples_leaf, min_gain_to_split, True)

    split_info, _ = _find_histogram_split(context, feature_idx, sample_indices)
    assert split_info.gain == -1

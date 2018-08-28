from statistics import median_high

import pytest

from plenum.test.helper import get_key_from_req

nodeCount = 7


@pytest.fixture(scope="module")
def tconf(tconf):
    old_throughput_measurement_class_name = tconf.throughput_measurement_class_name
    old_throughput_measurement_params = tconf.throughput_measurement_params
    old_min_latency_count = tconf.MIN_LATENCY_COUNT

    tconf.throughput_measurement_class_name = 'RevivalSpikeResistantEMAThroughputMeasurement'
    tconf.throughput_measurement_params = {
        'window_size': 5,
        'min_cnt': 2
    }
    tconf.MIN_LATENCY_COUNT = 1

    yield tconf

    tconf.MIN_LATENCY_COUNT = old_min_latency_count
    tconf.throughput_measurement_class_name = old_throughput_measurement_class_name
    tconf.throughput_measurement_params = old_throughput_measurement_params


def testThroughputThreshold(looper, txnPoolNodeSet, tconf, requests):
    looper.runFor(tconf.throughput_measurement_params['window_size'] *
                  tconf.throughput_measurement_params['min_cnt'])
    for node in txnPoolNodeSet:
        masterThroughput, avgBackupThroughput = node.monitor.getThroughputs(
            node.instances.masterId)
        for r in node.replicas:
            print("{} stats: {}".format(r, repr(r.stats)))
        assert masterThroughput / avgBackupThroughput >= node.monitor.Delta


def testReqLatencyThreshold(looper, txnPoolNodeSet, requests):
    for node in txnPoolNodeSet:
        for rq in requests:
            key = get_key_from_req(rq)
            assert key in node.monitor.masterReqLatenciesTest
            assert node.monitor.masterReqLatenciesTest[key] <= node.monitor.Lambda

from enum import Enum
from typing import Dict, Optional, Tuple

from plenum.common.channel import TxChannel, RxChannel, create_direct_channel, Router
from plenum.common.constants import POOL_LEDGER_ID, AUDIT_LEDGER_ID, AUDIT_TXN_LEDGERS_SIZE, AUDIT_TXN_LEDGER_ROOT, \
    AUDIT_TXN_VIEW_NO, AUDIT_TXN_PP_SEQ_NO
from plenum.common.ledger import Ledger
from plenum.common.metrics_collector import MetricsCollector
from plenum.common.timer import TimerService
from plenum.common.txn_util import get_payload_data
from plenum.server.catchup.ledger_leecher_service import LedgerLeecherService
from plenum.server.catchup.utils import CatchupDataProvider, LedgerCatchupComplete, NodeCatchupComplete, CatchupTill
from stp_core.common.log import getlogger

logger = getlogger()


class NodeLeecherService:
    class State(Enum):
        Idle = 0
        SyncingAudit = 1
        SyncingPool = 2
        SyncingOthers = 3

    def __init__(self,
                 config: object,
                 input: RxChannel,
                 output: TxChannel,
                 timer: TimerService,
                 metrics: MetricsCollector,
                 provider: CatchupDataProvider):
        self._config = config
        self._input = input
        self._output = output
        self._timer = timer
        self.metrics = metrics
        self._provider = provider

        self._state = self.State.Idle
        self._catchup_till = {}  # type: Dict[int, CatchupTill]

        # TODO: Get rid of this, theoretically most ledgers can be synced in parallel
        self._current_ledger = None  # type: Optional[int]

        self._leecher_outbox, self._leecher_outbox_rx = create_direct_channel()
        Router(self._leecher_outbox_rx).add(LedgerCatchupComplete, self._on_ledger_catchup_complete)
        self._leecher_outbox_rx.subscribe(lambda msg: output.put_nowait(msg))

        self._leechers = {}  # type: Dict[int, LedgerLeecherService]

    def __repr__(self):
        return "{}:NodeLeecherService".format(self._provider.node_name())

    def register_ledger(self, ledger_id: int):
        self._leechers[ledger_id] = \
            LedgerLeecherService(ledger_id=ledger_id,
                                 config=self._config,
                                 input=self._input,
                                 output=self._leecher_outbox,
                                 timer=self._timer,
                                 metrics=self.metrics,
                                 provider=self._provider)

    def start(self, request_ledger_statuses: bool):
        for leecher in self._leechers.values():
            leecher.reset()

        self._state = self.State.SyncingAudit
        self._catchup_till.clear()
        self._leechers[AUDIT_LEDGER_ID].start(request_ledger_statuses)

    def num_txns_caught_up_in_last_catchup(self) -> int:
        return sum(leecher.num_txns_caught_up for leecher in self._leechers.values())

    def _on_ledger_catchup_complete(self, msg: LedgerCatchupComplete):
        if self._state == self.State.SyncingAudit:
            self._on_audit_synced(msg)
        elif self._state == self.State.SyncingPool:
            self._on_pool_synced(msg)
        elif self._state == self.State.SyncingOthers:
            self._on_other_synced(msg)
        else:
            logger.warning("{} got unexpected catchup complete {} during idle state".format(self, msg))

    def _on_audit_synced(self, msg):
        if msg.ledger_id != AUDIT_LEDGER_ID:
            logger.warning("{} got unexpected catchup complete {} during syncing audit ledger".format(self, msg))
            return

        self._catchup_till = self._calc_catchup_till(msg.last_3pc)
        self._state = self.State.SyncingPool
        self._catchup_ledger(POOL_LEDGER_ID)

    def _on_pool_synced(self, msg):
        if msg.ledger_id != POOL_LEDGER_ID:
            logger.warning("{} got unexpected catchup complete {} during syncing pool ledger".format(self, msg))
            return

        self._state = self.State.SyncingOthers
        self._sync_next_ledger()

    def _on_other_synced(self, msg):
        if msg.ledger_id != self._current_ledger:
            logger.warning("{} got unexpected catchup complete {} during syncing ledger {}".
                           format(self, msg, self._current_ledger))
            return

        self._sync_next_ledger()

    def _sync_next_ledger(self):
        self._current_ledger = self._get_next_ledger(self._current_ledger)
        if self._current_ledger is not None:
            self._catchup_ledger(self._current_ledger)
        else:
            self._state = self.State.Idle
            self._output.put_nowait(NodeCatchupComplete())

    def _get_next_ledger(self, ledger_id: Optional[int]) -> Optional[int]:
        ledger_ids = list(self._leechers.keys())
        ledger_ids.remove(AUDIT_LEDGER_ID)
        ledger_ids.remove(POOL_LEDGER_ID)

        if len(ledger_ids) == 0:
            return None

        if ledger_id is None:
            return ledger_ids[0]

        next_index = ledger_ids.index(ledger_id) + 1
        if next_index == len(ledger_ids):
            return None

        return ledger_ids[next_index]

    def _catchup_ledger(self, ledger_id: int):
        leecher = self._leechers[ledger_id]
        catchup_till = self._catchup_till.get(ledger_id)
        if catchup_till is None:
            leecher.start(request_ledger_statuses=True)
        else:
            leecher.start(request_ledger_statuses=False, till=catchup_till)

    def _calc_catchup_till(self, last_3pc: Optional[Tuple[int, int]]) -> Dict[int, CatchupTill]:
        audit_ledger = self._provider.ledger(AUDIT_LEDGER_ID)
        last_audit_txn = audit_ledger.get_last_committed_txn()
        if last_audit_txn is None:
            return {}

        catchup_till = {}
        last_audit_txn = get_payload_data(last_audit_txn)
        view_no, pp_seq_no = last_3pc if last_3pc else (0, 0)
        for ledger_id, final_size in last_audit_txn[AUDIT_TXN_LEDGERS_SIZE].items():
            ledger = self._provider.ledger(ledger_id)
            start_size = ledger.size

            final_hash = last_audit_txn[AUDIT_TXN_LEDGER_ROOT].get(ledger_id)
            if final_hash is None:
                if final_size != ledger.size:
                    logger.error("{} has corrupted audit ledger: "
                                 "it indicates that ledger {} has new transactions but doesn't have new txn root".
                                 format(self, ledger_id))
                    return {}
                final_hash = Ledger.hashToStr(ledger.tree.root_hash) if final_size > 0 else None

            if isinstance(final_hash, int):
                audit_txn = audit_ledger.getBySeqNo(audit_ledger.size - final_hash)
                if audit_txn is None:
                    logger.error("{} has corrupted audit ledger: "
                                 "its txn root for ledger {} references nonexistent txn with seq_no {} - {} = {}".
                                 format(self, ledger_id, audit_ledger.size, final_hash, audit_ledger.size - final_hash))
                    return {}
                audit_txn = get_payload_data(audit_txn)
                final_hash = audit_txn[AUDIT_TXN_LEDGER_ROOT].get(ledger_id)
                if not isinstance(final_hash, str):
                    logger.error("{} has corrupted audit ledger: "
                                 "its txn root for ledger {} references txn with seq_no {} - {} = {} "
                                 "which doesn't contain txn root".
                                 format(self, ledger_id, audit_ledger.size, final_hash, audit_ledger.size - final_hash))
                    return {}

            catchup_till[ledger_id] = CatchupTill(start_size=start_size,
                                                  final_size=final_size,
                                                  final_hash=final_hash,
                                                  view_no=view_no,
                                                  pp_seq_no=pp_seq_no)

        return catchup_till

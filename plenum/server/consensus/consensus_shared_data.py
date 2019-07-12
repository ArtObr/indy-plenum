from typing import List

from sortedcontainers import SortedListWithKey

from plenum.common.messages.node_messages import Checkpoint, PrePrepare
from plenum.server.quorums import Quorums


class ConsensusSharedData:
    """
    This is a 3PC-state shared between Ordering, Checkpoint and ViewChange services.
    TODO: Consider depending on audit ledger
    TODO: Consider adding persistent local storage for 3PC certificates
    TODO: Restore primary name from audit ledger instead of passing through constructor
    """

    def __init__(self, name: str, validators: List[str], replica_index):
        self._name = name
        self.replica_index = replica_index
        self.view_no = 0
        self.last_ordered_3pc = (0, 0)
        self.pp_seq_no = 0
        self.waiting_for_new_view = False
        self.primary_name = None
        self.set_validators(validators)
        self.stable_checkpoint = None
        self.checkpoints = SortedListWithKey(key=lambda checkpoint: checkpoint.seqNoEnd)
        self.preprepared = []  # type:  List[PrePrepare]
        self.prepared = []  # type:  List[PrePrepare]

    @property
    def name(self) -> str:
        return self._name

    def set_validators(self, validators: List[str]):
        self._validators = validators
        self._quorums = Quorums(len(validators))

    @property
    def validators(self) -> List[str]:
        """
        List of validator nodes aliases
        """
        return self._validators

    @property
    def quorums(self) -> Quorums:
        """
        List of quorums
        """
        return self._quorums

    @property
    def is_primary(self) -> bool:
        return self.primary_name == self.name

    @property
    def last_checkpoint(self) -> Checkpoint:
        return self.checkpoints[-1]
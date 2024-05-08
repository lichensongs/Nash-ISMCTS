import numpy as np

import abc
from typing import Dict, List, Optional, Tuple


NUM_PLAYERS = 2
c_PUCT = 1.0
PHI_EPS = 0.05

Action = int
ValueArray = np.ndarray  # shape of (NUM_PLAYERS, )
IntervalArray = np.ndarray  # shape of (NUM_PLAYERS, 2)
IntervalArrayLike = IntervalArray | ValueArray
HiddenValue = int


DUMMY_INTERVAL_ARRAY = np.zeros((NUM_PLAYERS, 2))


def to_interval_array(i: IntervalArrayLike) -> IntervalArray:
    assert isinstance(i, np.ndarray), i
    if i.ndim == 1:
        return np.stack([i, i], axis=1)
    return i.copy()


class InfoSet(abc.ABC):
    @abc.abstractmethod
    def has_hidden_info(self) -> bool:
        pass

    @abc.abstractmethod
    def get_current_player(self) -> int:
        pass
    
    @abc.abstractmethod
    def get_game_outcome(self) -> Optional[int]:
        pass
    
    @abc.abstractmethod
    def get_actions(self) -> List[Action]:
        pass
    
    @abc.abstractmethod
    def get_H_mask(self) -> np.ndarray:
        pass
    
    @abc.abstractmethod
    def apply(self, action: Action) -> 'InfoSet':
        pass
    
    @abc.abstractmethod
    def instantiate_hidden_state(self, h: HiddenValue) -> 'InfoSet':
        pass
    
    

class Model(abc.ABC):
    # add abstract methods
    @abc.abstractmethod
    def action_eval(self, info_set: InfoSet) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        pass
    
    @abc.abstractmethod
    def hidden_eval(self, info_set: InfoSet) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        pass


class Node(abc.ABC):
    def __init__(self, info_set: InfoSet, Q: IntervalArrayLike):
        self.info_set = info_set
        self.cp = info_set.get_current_player()
        self.game_outcome = info_set.get_game_outcome()
        self.Q: IntervalArray = to_interval_array(Q)
        self.N = 0

    def terminal(self) -> bool:
        return self.game_outcome is not None

    @abc.abstractmethod
    def visit(self, model: Model):
        pass


class ActionNode(Node):
    def __init__(self, info_set: InfoSet, initQ: IntervalArrayLike=DUMMY_INTERVAL_ARRAY):
        super().__init__(info_set, initQ)

        self.actions = info_set.get_actions()
        self.children: Dict[Action, Node] = {}
        self.num_actions = len(self.actions)
        self.P = None
        self.V = None  #self.game_outcome if self.terminal() else None
        self.Vc = None

        if self.terminal():
            self.Q = to_interval_array(self.game_outcome)

        self.PURE = np.zeros(self.num_actions)
        self.MIXED = np.zeros(self.num_actions)
        self.n_mixed = 0
        self.n_pure = 0

    def expand(self, model: Model):
        self.P, self.V, self.Vc = model.action_eval(self.info_set)
        self.Q = to_interval_array(self.V)

        for a in self.actions:
            info_set = self.info_set.apply(a)
            if self.cp != info_set.get_current_player() and info_set.has_hidden_info():
                self.children[a] = SamplingNode(info_set, self.Vc[a])
            else:
                self.children[a] = ActionNode(info_set, self.Vc[a])

    def computePUCT(self):
        """
        Returns Q, selected_action_indices

        where:

        - Q has shape (self.num_actions, NUM_PLAYERS, 2)
        - selected_action_indices is a 1D array
        """
        cp = self.info_set.get_current_player()
        c = len(self.children)
        actions = np.zeros(c, dtype=int)
        Q = np.zeros((c, NUM_PLAYERS, 2))  # 2 for mins and maxes
        P = self.P
        N = np.zeros(c)
        for i, (a, child) in enumerate(self.children.items()):
            actions[i] = a
            Q[i] = child.Q
            N[i] = child.N

        PUCT = Q[:, cp] + c_PUCT * P * np.sqrt(np.sum(N)) / (N + 1)

        # check for pure case
        max_lower_bound_index = np.argmax(PUCT[:, 0])
        max_lower_bound = PUCT[max_lower_bound_index, 0]
        selected_action_indices = np.where(PUCT[:, 1] >= max_lower_bound)[0]
        return Q, selected_action_indices

    def get_mixing_distribution(self, action_indices):
        mask = np.zeros_like(P)
        mask[action_indices] = 1
        P = self.P * mask

        s = np.sum(P)
        assert s > 0
        return P / s

    def visit(self, model: Model):
        self.N += 1

        if self.terminal():
            return

        if self.P is None:
            self.expand(model)
            return

        Qc, action_indices = self.computePUCT()
        if len(action_indices) == 1:  # pure case
            self.n_pure += 1
            action_index = action_indices[0]
            pure_distr = np.zeros(len(self.P))
            pure_distr[action_index] = 1
            self.PURE = (self.PURE * (self.n_pure-1) + pure_distr) / self.n_pure
        else:  # mixed case
            self.n_mixed += 1
            mixing_distr = self.get_mixing_distribution(action_indices)
            action_index = np.random.choice(len(self.P), p=mixing_distr)
            self.MIXED = (self.MIXED * (self.n_mixed-1) + mixing_distr) / self.n_mixed

        na = np.newaxis
        E_mixed = np.sum(Qc * self.MIXED[:, na, na], axis=0)
        E_pure = np.sum(Qc * self.PURE[:, na, na], axis=0)
        self.Q = (self.n_mixed * E_mixed + self.n_pure * E_pure) / (self.n_mixed + self.n_pure)
        assert self.Q.shape == (NUM_PLAYERS, 2)

        action = self.actions[action_index]
        self.children[action].visit(model)


class SamplingNode(Node):
    def __init__(self, info_set: InfoSet, initQ: IntervalArrayLike):
        super().__init__(info_set, initQ)
        self.H = None
        self.V = None
        self.Vc = None
        self.Qc = None
        self.H_mask = info_set.get_H_mask()
        assert np.any(self.H_mask)
        self.children: Dict[HiddenValue, Node] = {}
    
    def apply_H_mask(self):
        self.H *= self.H_mask

        H_sum = np.sum(self.H)
        if H_sum < 1e-6:
            self.H = self.H_mask / np.sum(self.H_mask)
        else:
            self.H /= H_sum

    @staticmethod
    def Phi(c: HiddenValue, eps: float, Q: np.ndarray, H: np.ndarray, verbose=False) -> IntervalArray:
        """
        H: shape of (n, )
        Q: shape of (n, NUM_PLAYERS, 2)

        H is a hidden state probability distribution.
        Q represents a utility-belief-interval for each hidden state, for each player.
        c is an index sampled from H.

        Computes:

        Phi(H) = union_{H' in N_epsilon(H)} phi(H')
        
        where 
        
        N_epsilon(H) = {H' | ||H - H'||_1 <= epsilon}
        Q_left = Q[:, :, 0]
        Q_right = Q[:, :, 1]
        phi(H') = union_{Q_left <= q <= Q_right} (q[c] - sum_i H'[i] * q[i])

        Returns the set Phi(H) as an IntervalArray.
        """
        one_c = np.zeros_like(H)
        one_c[c] = 1

        # TODO: move p-loop inside the index-loop, around the direction-loop
        output = np.zeros(NUM_PLAYERS, 2)
        for p in range(NUM_PLAYERS):
            for index in (0, 1):
                index_sign = 1 - 2 * index
                H_prime = H.copy()
                phi_partial_extreme = -Q[:, p, 1 - index]
                phi_partial_extreme[c] = -Q[c, p, index]
                index_ordering = np.argsort(phi_partial_extreme)

                for direction in (-1, 1):
                    eps_limit = (1 - H_prime) if direction == index_sign else H_prime
                    remaining_eps = eps
                    for i in index_ordering[::direction]:
                        eps_to_use = min(remaining_eps, eps_limit[i])
                        H_prime[i] += index_sign * direction * eps_to_use
                        remaining_eps -= eps_to_use
                        if remaining_eps <= 0:
                            break

                assert np.isclose(np.sum(H_prime), 1), H_prime

                q = Q[:, p, 1 - index].copy()
                q[c] = Q[c, p, index]
                output[p, index] = np.dot(one_c - H_prime, q)

        if verbose:
            print('*****')
            print('Phi computation:')
            print('H = %s' % H)
            print('Q = %s' % Q)
            print('c = %s' % c)
            print('eps = %s' % eps)
            print('Phi = %s' % output)
        return output

    def expand(self, model: Model):
        self.H, self.V, self.Vc = model.hidden_eval(self.info_set)
        self.apply_H_mask()

        self.Qc = np.zeros((len(self.H), NUM_PLAYERS, 2))
        for h in np.where(self.H_mask)[0]:
            info_set = self.info_set.instantiate_hidden_state(h)
            if info_set.has_hidden_info():
                assert self.cp == info_set.get_current_player()
                child = SamplingNode(info_set, self.Vc[h])
            else:
                child = ActionNode(info_set, self.Vc[h])

            self.children[h] = child
            self.Qc[h] = child.Q

        self.Q = to_interval_array(self.V)

    def visit(self, model: Model):
        self.N += 1

        if self.H is None:
            self.expand(model)

        c = np.random.choice(len(self.H), p=self.H)

        Phi = self.Phi(c, PHI_EPS, self.Qc, self.H)

        self.children[c].visit(model)

        
class Tree:
    def __init__(self, model: Model, root: ActionNode):
        self.model = model
        self.root = root

    def visit(self):
        self.root.visit()

import numpy as np
import random
from copy import deepcopy
import torch


class NeuralNetwork(torch.nn.Module):
    def __init__(
        self,
        d: int,
        L: int = 2,
        m: int = 20,
        random_seed: int = 12345,
        device: torch.device = torch.device("cpu"),
    ):
        """The proposed neural network structure in Zhou 2020

        Args:
            d (int): Dimension of input layer.
            L (int, optional): Number of Layers. Defaults to 2.
            m (int, optional): Width of each layer. Defaults to 20.
            random_seed (int, optional): rando_seed. Defaults to 12345.
            device (torch.device, optional): The device of calculateing tensor. Defaults to torch.device("cpu").
        """
        super().__init__()
        np.random.seed(random_seed)
        torch.manual_seed(random_seed)

        self.d = d
        self.L = L
        self.m = m
        self.random_seed = random_seed
        self.activation = torch.nn.ReLU()

        self.device = device
        print(f"Using device {self.device}")

        self.W = torch.nn.ParameterDict()
        w_for_1 = np.random.randn(d // 2, m // 2) * np.sqrt(4 / m)
        w_for_1_to_Lminus1 = np.random.randn(m // 2, m // 2) * np.sqrt(4 / m)
        w_for_L = np.random.randn(m // 2) * np.sqrt(2 / m)
        for layer_index in range(1, L + 1):
            if layer_index == 1:
                W = np.zeros((d, m))
                W[0 : d // 2, 0 : m // 2] = w_for_1
                W[d // 2 :, m // 2 :] = w_for_1
                self.W["W1"] = torch.nn.Parameter(torch.from_numpy(W)).to(self.device)
            elif layer_index == L:
                W = np.zeros((m, 1))
                W[0 : m // 2, 0] = w_for_L
                W[m // 2 :, 0] = -w_for_L
                self.W[f"W{layer_index}"] = torch.nn.Parameter(torch.from_numpy(W)).to(self.device)
            else:
                W = np.zeros((m, m))
                W[0 : m // 2, 0 : m // 2] = w_for_1_to_Lminus1
                W[m // 2 :, m // 2 :] = w_for_1_to_Lminus1
                self.W[f"W{layer_index}"] = torch.nn.Parameter(torch.from_numpy(W)).to(self.device)
        self.W0 = dict()
        for key in self.W.keys():
            self.W0[key] = deepcopy(self.W[key])
            self.W0[key].requires_grad_(requires_grad=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """we accept a Tensor of input data and we must return
        a Tensor of output data

        Args:
            x (torch.Tensor): The observed context of each arm

        Returns:
            torch.Tensor: The predicted mean reward of each arm
        """
        if len(x.shape) == 1:
            x = x[None, :]

        assert x.shape[1] == self.d, "Dimension doesn't match"
        x = x.to(self.device)
        for layer_index in range(1, self.L + 1):
            x = torch.matmul(x, self.W[f"W{layer_index}"])
            if layer_index != self.L:
                x = self.activation(x)
        x = x * np.sqrt(self.m)
        return x

    def GetGrad(self, x: torch.tensor) -> np.ndarray:
        """Given the vector of context, return the flattern gradient of parameter

        Args:
            x (torch.tensor): x.shape = (d,)

        Returns:
            np.ndarray: The gradient of parameter at given point
        """
        x = x[None, :]  # expand the dimension of x
        output = self.forward(x)[0, 0]
        output.backward()

        grad = np.array([])
        for para in self.parameters():
            grad = np.concatenate([grad, para.grad.cpu().detach().numpy().flatten()], axis=0)
        return grad


class NeuralAgent:
    def __init__(
        self,
        K: int,
        T: int,
        d: int,
        L: int = 2,
        m: int = 20,
        nu: float = 0.1,
        lambda_: float = 1,
        eta: float = 0.01,
        frequency: int = 50,
        batchsize: int = None,
        random_seed: int = 12345,
    ):
        """_summary_

        Args:
            K (int): Total number of actions
            T (int): Total number of periods
            d (int): The dimension of context
            L (int, optional): The number of hidden layer. Defaults to 2.
            m (int, optional): The number of neurals in each layer. Defaults to 20.
            nu (float, optional): Scale coefficient of variance. Defaults to 0.1.
            lambda_ (float, optional): Regularization of regression problem. Defaults to 1.
            eta (float, optional): Step size of the SGD. Defaults to 0.01.
            frequency (int, optional): The interval between two training epoches. Defaults to 50.
            batchsize (int, optional): The size of sample batch in SGD. Defaults to None.
            random_seed (int, optional) : The random seed of. Defaults to 12345
        """
        # random seed
        self.random_generator = random.Random()
        self.random_generator.seed(random_seed)
        self.random_seed = random_seed

        # the setup of the problem
        self.K = K
        self.T = T

        # the setup of neural network
        self.L = L
        self.m = m
        self.d = d
        self.p = m + m * d + m * m * (L - 2)
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.mynn = NeuralNetwork(d=d, L=L, m=m, device=self.device)
        self.optimizer = torch.optim.SGD(self.mynn.parameters(), lr=eta)
        self.criterion = torch.nn.MSELoss()

        # the setup of the trainning process
        self.nu = nu
        self.lambda_ = lambda_
        self.eta = eta
        self.frequency = frequency
        self.batchsize = batchsize
        self.U = lambda_ * np.eye(self.p)

        # the history
        self.t = 0  # marks the index of period
        self.history_reward = np.zeros(T)
        self.history_action = np.zeros(T)
        self.predicted_reward = np.zeros(T)
        self.history_context = np.zeros((T, d))

    def Action(self, context_list: np.array) -> int:
        """Given the observed context of each arm, return the predicted arm

        Args:
            context_list (np.array): The observed context of each arm. context_list.shape = (K, d)

        Returns:
            int: the index of predicted arm, take value from 0, 1, ..., K-1
        """
        sample_reward_ = np.zeros(self.K)
        predict_reward_ = np.zeros(self.K)
        U_inverse = np.linalg.inv(self.U)
        for arm in range(1, self.K + 1):
            grad_arm = self.mynn.GetGrad(torch.from_numpy(context_list[arm - 1, :]))
            sigma_t_k2 = self.lambda_ * grad_arm.dot(U_inverse).dot(grad_arm) / self.m
            predict_reward_[arm - 1] = self.mynn.forward(torch.from_numpy(context_list[arm - 1, :]))
            sample_reward_[arm - 1] = self.random_generator.normalvariate(mu=predict_reward_[arm - 1], sigma=np.sqrt(self.nu**2 * sigma_t_k2))
        ind = np.argmax(sample_reward_)

        # save the history
        self.history_action[self.t] = ind
        self.history_context[self.t, :] = context_list[ind, :]
        self.predicted_reward[self.t] = predict_reward_[ind]

        return ind

    def Update(self, reward):
        self.history_reward[self.t] = reward
        ind = self.history_action[self.t]
        context = self.history_context[self.t, :]

        # update U
        grad_parameter = self.mynn.GetGrad(torch.from_numpy(context))
        self.U = self.U + grad_parameter.dot(grad_parameter.transpose()) / self.m

        # train the network
        if (self.t + 1) % self.frequency == 0:
            # initialize the network again
            for key in self.mynn.W.keys():
                self.mynn.W[key].data = deepcopy(self.mynn.W0[key].data)

            # for jj in range(self.t):  ## J=t at round t, but when we adopt such setting, the training process will be very slow
            for jj in range(np.minimum(self.t, 100)):
                loss_ = list()

                # shuffle the history and conduct SGD
                history_index = np.arange(self.t + 1)
                np.random.shuffle(history_index)
                temp_history_context = self.history_context[history_index, :]
                temp_history_reward = self.history_reward[history_index]
                for batch_index in range(0, self.t // self.batchsize + 1):
                    # split the batch
                    if batch_index < self.t // self.batchsize:
                        X_temp = torch.from_numpy(temp_history_context[batch_index * self.batchsize : (batch_index + 1) * self.batchsize, :]).to(self.device)
                        y_temp = torch.from_numpy(temp_history_reward[batch_index * self.batchsize : (batch_index + 1) * self.batchsize]).to(self.device)
                    else:
                        X_temp = torch.from_numpy(temp_history_context[batch_index * self.batchsize :, :]).to(self.device)
                        y_temp = torch.from_numpy(temp_history_reward[batch_index * self.batchsize :]).to(self.device)

                    # update the neural network
                    self.optimizer.zero_grad()
                    output = self.mynn.forward(X_temp)

                    # calculate the loss function
                    # in their orginal paper, $loss(\theta)=\sum_{i=1}^t(f(x_{i,a_i}, \theta)-r_{i,a_i})^2+m\lambda\|\theta-\theta^{(0)}\|_2^2/2$
                    # but here we set $loss(\theta)=\sum_{i=1}^t(f(x_{i,a_i}, \theta)-r_{i,a_i})^2/t+\lambda\|\theta-\theta^{(0)}\|_2^2/2/p$
                    # to balance the terms in the loss function
                    loss = self.criterion(output[:, 0], y_temp)  ## predict error
                    # loss = torch.sum((output[:, 0] - y_temp) ** 2)
                    ## regularization
                    for key in self.mynn.W.keys():
                        # loss += self.lambda_ * self.m * torch.sum((self.mynn.W[key] - self.mynn.W0[key]) ** 2) / 2
                        loss += self.lambda_ * torch.sum((self.mynn.W[key] - self.mynn.W0[key]) ** 2) / 2 / self.p
                    loss.backward()
                    self.optimizer.step()

                    # record the training process
                    loss_.append(loss.cpu().detach().numpy())

                if (jj + 1) % 20 == 0:
                    print(f"{jj+1} training epoch, mean loss value is {np.mean(loss_)}")

        self.t += 1


class BestAgent:
    def __init__(self, K, T, d, A):
        # K is Total number of actions,
        # T is Total number of periods
        # d is the dimension of context
        # A is the context
        self.K = K
        self.T = T
        self.d = d
        self.t = 0  # marks the index of period
        self.A = A
        self.history_reward = np.zeros(T)
        self.history_action = np.zeros(T)
        self.history_context = np.zeros((d, T))

    def Action(self, context_list):
        # context_list is a d*K matrix, each column represent a context
        # the return value is the action we choose, represent the index of action, is a scalar

        expected_reward = np.zeros(self.K)
        for kk in range(0, self.K):
            context = context_list[kk, :]
            expected_reward[kk] = context.transpose().dot(self.A.transpose().dot(self.A)).dot(context)
        ind = np.argmax(expected_reward, axis=None)
        self.history_context[:, self.t] = context_list[ind, :]
        self.history_action[self.t] = ind
        return ind

    def Update(self, reward):
        # reward is the realized reward after we adopt policy, a scalar
        self.history_reward[self.t] = reward
        self.t = self.t + 1

    def GetHistoryReward(self):
        return self.history_reward

    def GetHistoryAction(self):
        return self.history_action

    def GetHistoryContext(self):
        return self.history_context


class UniformAgent:
    def __init__(self, K, T, d):
        # K is Total number of actions,
        # T is Total number of periods
        # d is the dimension of context
        self.K = K
        self.T = T
        self.d = d
        self.t = 0  # marks the index of period
        self.history_reward = np.zeros(T)
        self.history_action = np.zeros(T)
        self.history_context = np.zeros((d, T))

    def Action(self, context_list):
        # context_list is a d*K matrix, each column represent a context
        # the return value is the action we choose, represent the index of action, is a scalar

        ind = np.random.randint(0, high=self.K)  # we just uniformly choose an action
        self.history_context[:, self.t] = context_list[ind, :]
        return ind

    def Update(self, reward):
        # reward is the realized reward after we adopt policy, a scalar
        self.history_reward[self.t] = reward
        self.t = self.t + 1

    def GetHistoryReward(self):
        return self.history_reward

    def GetHistoryAction(self):
        return self.history_action

    def GetHistoryContext(self):
        return self.history_context


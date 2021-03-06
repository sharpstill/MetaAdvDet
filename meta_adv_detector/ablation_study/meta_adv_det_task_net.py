import sys
from collections import OrderedDict

sys.path.append("/home1/machen/adv_detection_meta_learning")

from networks.conv3 import Conv3

import os
import copy
from config import PY_ROOT, IN_CHANNELS, IMAGE_SIZE
from torch.optim import Adam,SGD
from torch.utils.data import DataLoader
from dataset.meta_task_dataset import MetaTaskDataset
from networks.resnet import resnet10, resnet18
from meta_adv_detector.score import *

from meta_adv_detector.tensorboard_helper import TensorBoardWriter

# 只要一个网络，但是在support上和query上更新
class MetaLearnerTasknetOnly(object):
    def __init__(self,
                 dataset,
                 num_classes,
                 meta_batch_size,
                 meta_step_size,
                 inner_step_size, lr_decay_itr,
                 epoch,
                 num_inner_updates, load_task_mode, protocol, arch,
                 tot_num_tasks, num_support, num_query, no_random_way,
                 tensorboard_data_prefix, train=True, adv_arch="conv3",need_val=False):
        super(self.__class__, self).__init__()
        self.dataset = dataset
        self.num_classes = num_classes
        self.meta_batch_size = meta_batch_size  # task number per batch
        self.meta_step_size = meta_step_size
        self.inner_step_size = inner_step_size
        self.lr_decay_itr = lr_decay_itr
        self.epoch = epoch
        self.num_inner_updates = num_inner_updates
        self.test_finetune_updates = num_inner_updates
        # Make the nets
        if arch == "conv3":
            # network = FourConvs(IN_CHANNELS[self.dataset_name], IMAGE_SIZE[self.dataset_name], num_classes)
            network = Conv3(IN_CHANNELS[self.dataset], IMAGE_SIZE[self.dataset], num_classes)
        elif arch == "resnet10":
            network = resnet10(num_classes, pretrained=False)
        elif arch == "resnet18":
            network = resnet18(num_classes, pretrained=False)
        self.network = network
        self.network.cuda()
        if train:
            trn_dataset = MetaTaskDataset(tot_num_tasks, num_classes, num_support, num_query,
                                          dataset, is_train=True, load_mode=load_task_mode,
                                          protocol=protocol,
                                          no_random_way=no_random_way, adv_arch=adv_arch)
            self.train_loader = DataLoader(trn_dataset, batch_size=meta_batch_size, shuffle=True, num_workers=0, pin_memory=True)
            self.tensorboard = TensorBoardWriter("{0}/MAML_task_net_tensorboard".format(PY_ROOT),
                                                 tensorboard_data_prefix)
            os.makedirs("{0}/MAML_task_net_tensorboard".format(PY_ROOT), exist_ok=True)
        if need_val:
            val_dataset = MetaTaskDataset(tot_num_tasks, num_classes, num_support, 15,
                                          dataset, is_train=False, load_mode=load_task_mode,
                                          protocol=protocol,
                                          no_random_way=True, adv_arch=adv_arch)
            self.val_loader = DataLoader(val_dataset, batch_size=100, shuffle=False, num_workers=0, pin_memory=True) # 固定100个task，分别测每个task的准确率

        self.opt = Adam(self.network.parameters(), lr=meta_step_size)




    def test_task_F1(self, iter=0, limit=100):
        test_net = copy.deepcopy(self.network)
        # Select ten tasks randomly from the test set to evaluate_accuracy on
        support_F1_list, query_F1_list = [], []
        all_c = 0
        for support_images, _, support_labels, query_images, _, query_labels, positive_position in self.val_loader:
            for task_idx in range(support_images.size(0)):  # 选择100个task
                # Make a test net with same parameters as our current net
                test_net.copy_weights(self.network)
                test_net.cuda()
                test_net.train()
                test_opt = SGD(test_net.parameters(), lr=self.inner_step_size)
                # for m in test_net.modules():
                #     if isinstance(m, torch.nn.BatchNorm2d):
                #         m.eval()
                finetune_img, finetune_target = support_images[task_idx].cuda(), support_labels[task_idx].cuda()
                for i in range(self.test_finetune_updates):  # 先fine_tune
                    loss, _  = forward_pass(test_net, finetune_img, finetune_target)
                    # print(loss.item())
                    test_opt.zero_grad()
                    loss.backward()
                    test_opt.step()
                # print("---------")
                test_net.eval()
                # Evaluate the trained model on train and val examples
                support_accuracy, support_F1 = evaluate_two_way(test_net, finetune_img, finetune_target)
                query_accuracy, query_F1 = evaluate_two_way(test_net, query_images[task_idx], query_labels[task_idx])
                support_F1_list.append(support_F1)
                query_F1_list.append(query_F1)
                all_c += 1
                if limit >0 and all_c > limit:
                    break
        support_F1 = np.mean(support_F1_list)
        query_F1 = np.mean(query_F1_list)
        result_json = {"support_F1": support_F1,
                       "query_F1": query_F1,
                       "num_updates": self.num_inner_updates}
        if iter >= 0:
            query_F1_tensor = torch.Tensor(1)
            query_F1_tensor.fill_(query_F1)
            self.tensorboard.record_val_query_F1(query_F1_tensor, iter)
        print('Validation Set iteration:{} Support F1: {} Query F1: {}'.format(iter, support_F1, query_F1))
        del test_net
        return result_json


    def train(self, model_path, resume_epoch=0, need_val=False):
        for epoch in range(resume_epoch, self.epoch):
            # Evaluate on test tasks
            # Collect a meta batch update
            # Save a model snapshot every now and then

            for i, (support_images, _, support_labels, query_images, _, query_labels, _) in enumerate(self.train_loader):
                itr = epoch * len(self.train_loader) + i
                self.adjust_learning_rate(itr, self.meta_step_size, self.lr_decay_itr)
                support_images, support_labels, query_images, query_labels = support_images.cuda(), support_labels.cuda(), query_images.cuda(), query_labels.cuda()
                for task_idx in range(support_images.size(0)):  # 在每一个task依次更新即可，所以无需2个网络
                    in_support, in_query, target_support, target_query = support_images[task_idx], query_images[task_idx],\
                                                                         support_labels[task_idx], query_labels[task_idx]
                    fast_weights = OrderedDict((name, param) for (name, param) in self.network.named_parameters())
                    for i in range(self.num_inner_updates):
                        if i == 0:
                            loss, _ = self.network.forward_pass(in_support, target_support)
                            grads = torch.autograd.grad(loss, self.network.parameters())
                        else:
                            loss, _ = self.network.forward_pass(in_support, target_support, fast_weights)
                            grads = torch.autograd.grad(loss, fast_weights.values())
                        fast_weights = OrderedDict((name, param - self.inner_step_size * grad) for ((name, param), grad) in
                                                   zip(fast_weights.items(), grads))
                    # fast_net only forward one task's data
                    loss, _ = self.network.forward_pass(in_query, target_query, fast_weights)
                    loss = loss / self.meta_batch_size  # normalize loss
                    self.opt.zero_grad()
                    loss.backward()
                    self.opt.step()
                # Perform the meta update
                # print('Meta update', itr)

                if itr % 100 == 0 and need_val:
                    self.test_task_F1(itr, limit=200)
            torch.save({
                'epoch': epoch + 1,
                'state_dict': self.network.state_dict(),
                'optimizer': self.opt.state_dict(),
            }, model_path)


    def adjust_learning_rate(self,itr, meta_lr, lr_decay_itr):
        """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
        if lr_decay_itr > 0:
            if int(itr % lr_decay_itr) == 0 and itr > 0:
                meta_lr = meta_lr / (10 ** int(itr / lr_decay_itr))
                for param_group in self.opt.param_groups:
                    param_group['lr'] = meta_lr

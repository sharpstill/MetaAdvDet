import random
from collections import defaultdict

from torch.utils import data
import glob
import re
import numpy as np
import os
from lru import LRU
from dataset.protocol_enum import SPLIT_DATA_PROTOCOL


class AdversaryDataset(data.Dataset):
    def __init__(self, root_path, train, protocol, META_ATTACKER_PART_I, META_ATTACKER_PART_II, balance, use_cache=True):
        self.root_path = root_path
        self.use_cache = use_cache
        filter_str = "train"
        if not train:
            filter_str = "test"
        extract_pattern = re.compile("(.*?)_untargeted.*")
        self.cache = LRU(16)
        self.img_label_list = []
        self.img_label_dict = defaultdict(list)
        for npz_path in glob.glob(root_path + "/*{}.npz".format(filter_str)):

            ma = extract_pattern.match(os.path.basename(npz_path))
            adv_name = ma.group(1)
            if protocol == SPLIT_DATA_PROTOCOL.TRAIN_I_TEST_II and train:
                if adv_name not in META_ATTACKER_PART_I:
                    continue
            elif protocol == SPLIT_DATA_PROTOCOL.TRAIN_II_TEST_I and train:
                if adv_name not in META_ATTACKER_PART_II:
                    continue
            elif protocol == SPLIT_DATA_PROTOCOL.TRAIN_II_TEST_I and not train:
                if adv_name not in META_ATTACKER_PART_I:
                    continue
            elif protocol == SPLIT_DATA_PROTOCOL.TRAIN_I_TEST_II and not train:
                if adv_name not in META_ATTACKER_PART_II:
                    continue

            data = np.load(npz_path)
            adv_pred = data["adv_pred"]
            gt_label = data["gt_label"]

            if adv_name == "clean":
                adv_label = 1
                adv_images = data["adv_images"]
                if self.use_cache:
                    self.cache[npz_path] = adv_images
                indexes = np.arange(adv_images.shape[0])
            else:
                adv_label = 0
                indexes = np.where(adv_pred != gt_label)[0]
            for index in indexes:
                self.img_label_dict[adv_label].append((npz_path, index, adv_label))
            print("{} done".format(npz_path))
        self.img_label_list.extend(self.img_label_dict[1])
        if balance:
            self.img_label_list.extend(random.sample(self.img_label_dict[0], len(self.img_label_dict[1])))
        else:
            self.img_label_list.extend(self.img_label_dict[0])

    def __len__(self):
        return len(self.img_label_list)


    def __getitem__(self, item):
        npz_path, index, label = self.img_label_list[item]
        if self.use_cache and npz_path in self.cache:
            adv_images = self.cache[npz_path]
        else:
            data = np.load(npz_path)
            adv_images = data["adv_images"]  # 10000,32,32,3
            if self.use_cache:
                self.cache[npz_path] = adv_images

        adv_image = adv_images[index]
        adv_image = np.transpose(adv_image, (2,0,1))

        return adv_image, label

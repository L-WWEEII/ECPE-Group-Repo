import os
import csv
from config import *
import torch
from torch.nn import functional as F
from torch.utils.data import Dataset, DataLoader
from transformers import AdamW, get_linear_schedule_with_warmup, BertTokenizer, BertForSequenceClassification
from model import Network
import datetime
import numpy as np
import pandas as pd


class Discourse(Dataset):
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.data_path = "data/test/discourse.csv"
        self.discourses_list = []
        df = pd.read_csv(self.data_path)
        for i in range(len(df)):
            section = int(df['section'][i])
            discourse = df['discourse'][i]
            word_count = int(df['word_count'][i])
            doc_len = int(df['doc_len'][i])
            clause_len = df['clause_len'][i]
            emotion_pos = df['emotion_pos'][i]
            cause_pos = df['cause_pos'][i]
            self.discourses_list.append([section, discourse, word_count, doc_len, clause_len, emotion_pos, cause_pos])

    def __getitem__(self, item):
        # line = self.discourses_list[item]
        # encoding = self.tokenizer(line[1], return_tensors='pt', padding='max_length', truncation=True, max_length=512)
        # return {'input_ids': encoding['input_ids'].squeeze(),
        #         'token_type_ids': encoding['token_type_ids'].squeeze(),
        #         'attention_mask': encoding['attention_mask'].squeeze(),
        #         }
        item = self.discourses_list[item]
        item[1] = torch.Tensor(self.tokenizer(item[1],padding='max_length',max_length=512)['input_ids']).to(torch.int32)
        # print(item[1])
        return item

    def __len__(self):
        return len(self.discourses_list)


class PairsWithConnectivesDataSet(Dataset):
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.data_path = "data/test/pairs_withconn&possibility.csv"
        self.pairs_list = []
        df = pd.read_csv(self.data_path)
        for i in range(len(df)):
            section = int(df['section'][i])
            emo_clause_index = df['emo_clause_index'][i]
            cau_candidate_index = df['cau_candidate_index'][i]
            emotion_clause = df['emotion_clause'][i]
            cause_candidate = df['cause_candidate'][i]
            conn_words = df['conn_words'][i]
            possibility_distribution = df['possibility_distribution'][i]
            correctness = df['correctness'][i]
            self.pairs_list.append(
                [section, emo_clause_index, cau_candidate_index, emotion_clause, cause_candidate, conn_words,
                 possibility_distribution, correctness, ''])

    def __getitem__(self, item):
        item = self.pairs_list[item]
        item[7] = 0 if item[7] == 'false' else 1
        item[5] = ''.join(word for word in eval(item[5]))
        item[8] = torch.Tensor(self.tokenizer(item[5] + '[CLS]' + item[3] + item[4], padding='max_length',max_length=512)['input_ids']).to(torch.int32)
        print('item[5]', item[5])
        print('item[8]', item[8])
        return item


    def __len__(self):
        return len(self.pairs_list)


def main(configs, discourse_dataloader, pair_dataloader, tokenizer):
    torch.manual_seed(TORCH_SEED)
    torch.cuda.manual_seed_all(TORCH_SEED)
    torch.backends.cudnn.deterministic = True

    # model
    model = Network(configs).to(DEVICE)
    # optimizer
    params = list(model.named_parameters())
    optimizer_grouped_params = [
        {'params': [p for n, p in params if '_bert' in n], 'weight_decay': 0.01},
        {'params': [p for n, p in params if '_bert' not in n], 'lr': configs.lr, 'weight_decay': 0.01}
    ]
    optimizer = AdamW(params=optimizer_grouped_params, lr=configs.tuning_bert_rate)

    # scheduler
    training_steps = configs.epochs * len(discourse_dataloader) // configs.gradient_accumulation_steps
    warmup_steps = int(training_steps * configs.warmup_proportion)
    scheduler = get_linear_schedule_with_warmup(optimizer=optimizer, num_warmup_steps=warmup_steps,
                                                num_training_steps=training_steps)

    # training
    model.zero_grad()
    early_stop_flag = None
    pair_dataloader_header = 0

    for epoch in range(1, configs.epochs + 1):
        for train_step in range(1, len(discourse_dataloader) + 1):
            model.train()

            # Need debug
            # TypeError: 'DataLoader' object is not subscriptable
            batch_discourse = discourse_dataloader[train_step]

            section, discourse, word_count, doc_len, clause_len, emotion_pos, cause_pos = batch_discourse

            emotion_pos = eval(emotion_pos[0])
            discourse_mask = torch.Tensor([1] * word_count + [0] * (512 - word_count)).to(torch.int32)
            # segment_mask = torch.Tensor([0] * word_count)
            segment_mask = torch.Tensor([0] * 512).to(torch.int32)

            query_len = 0
            # discourse_adj = torch.ones([doc_len, doc_len]).unsqueeze(0)  # batch size = 1
            discourse_adj = torch.ones([doc_len, doc_len])  # batch size = 1
            emo_ans = torch.zeros(doc_len)
            for pos in emotion_pos:
                emo_ans[int(pos) - 1] = 1
            # emo_ans = torch.nn.functional.one_hot(torch.tensor([int(pos) - 1 for pos in emotion_pos]),
            #                                       num_classes=doc_len.item())
            emo_ans_mask = torch.ones(doc_len)  # batch size = 1
            # print(discourse_mask.unsqueeze(0))

            pair_count = len(emotion_pos) * (doc_len - len(emotion_pos) + 1)

            # Need debug
            # TypeError: 'DataLoader' object is not subscriptable
            batch_pair = pair_dataloader[pair_dataloader_header:pair_dataloader_header+pair_count]

            section, emo_clause_index, cau_candidate_index, _, _, _, possibility_distribution, correctness, tokenized_sequence = [None for i in range(pair_count)]
            for i in range(pair_count):
                section[i], emo_clause_index[i], cau_candidate_index[i], _, _, _, possibility_distribution[i], correctness[i], tokenized_sequence[i] = batch_pair[pair_dataloader_header+i]

            emo_cau_ans = torch.zeros(len(emotion_pos) * doc_len)
            for i in range(len(emotion_pos)):
                for j in range(len(cause_pos)):
                    emo_cau_ans[doc_len * i + cause_pos[j] - 1] = 1
            emo_cau_ans_mask = torch.ones(len(emotion_pos) * doc_len)

            emo_pred = model(discourse, discourse_mask.unsqueeze(0), segment_mask.unsqueeze(0), query_len, emotion_pos, cause_pos, clause_len, doc_len, discourse_adj, possibility_distribution, correctness, tokenized_sequence, 'emo')
            emo_cau_pred = model(discourse, discourse_mask.unsqueeze(0), segment_mask.unsqueeze(0), query_len, emotion_pos, cause_pos, clause_len, doc_len, discourse_adj, possibility_distribution, correctness, tokenized_sequence, 'emo_cau')

            loss_emo = model.loss_pre_emo(emo_pred, emo_ans, emo_ans_mask)
            loss_emo_cau = model.loss_emo_cau(emo_cau_pred, emo_cau_ans, emo_cau_ans_mask, possibility_distribution)
            loss = loss_emo + loss_emo_cau
            loss.backward()

            if train_step % configs.gradient_accumulation_steps == 0:
                optimizer.step()
                scheduler.step()
                model.zero_grad()

            if train_step % 1 == 0:
                print('epoch: {}, step: {}, loss_emo: {}, loss_emo_cau: {}, loss: {}'
                    .format(epoch, train_step, loss_emo, loss_emo_cau, loss))

    return None


if __name__ == '__main__':
    configs = Config()
    device = DEVICE
    tokenizer = BertTokenizer.from_pretrained(configs.bert_cache_path)
    model = Network(configs).to(DEVICE)
    discourse_dataset = Discourse(tokenizer)
    discourse_dataloader = DataLoader(dataset=discourse_dataset, shuffle=False, batch_size=1)
    pair_dataset = PairsWithConnectivesDataSet(tokenizer)
    pair_dataloader = DataLoader(dataset=pair_dataset, shuffle=False, batch_size=1)

    main(configs, discourse_dataloader, pair_dataloader, tokenizer)
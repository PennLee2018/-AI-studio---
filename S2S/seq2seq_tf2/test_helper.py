import tensorflow as tf
import numpy as np
from seq2seq_tf2.batcher import output_to_words
from tqdm import tqdm
import math

TEST_NUM_SAMPLES = 20000

def greedy_decode(model, dataset, vocab, params):
    # 存储结果
    batch_size = params["batch_size"]
    results = []

    sample_size = TEST_NUM_SAMPLES #20000(小样本数50)
    # batch 操作轮数 math.ceil向上取整 小数 +1
    # 因为最后一个batch可能不足一个batch size 大小 ,但是依然需要计算
    steps_epoch = sample_size // batch_size + 1
    
    #此处可显示进度
    for i in tqdm(range(steps_epoch)):
        enc_data, _ = next(iter(dataset))#数据一批批送入
        results += batch_greedy_decode(model, enc_data, vocab, params)
    return results


def batch_greedy_decode(model, enc_data, vocab, params):
    # 判断输入长度
    batch_data = enc_data["enc_input"]#shape=(batch_size,实际输入序列长度)
    batch_size = enc_data["enc_input"].shape[0]
    # 开辟结果存储list
    predicts = [''] * batch_size#也是一批一起运算，大小为batch_size
    #print("batch_data,batch_size,predicts",batch_data,batch_size,predicts)
    # inputs = batch_data # shape=(batch_size,实际序列长度)
    inputs = tf.convert_to_tensor(batch_data)#
    # hidden = [tf.zeros((batch_size, params['enc_units']))]
    # enc_output, enc_hidden = model.encoder(inputs, hidden)
    enc_output, enc_hidden = model.call_encoder(inputs)#enc_output.shape=(batch_size,实际长度,enc_hidden_size),enc_hidden.shape=dec_hidden.shape=(batch_size,hidden_size)

    dec_hidden = enc_hidden
    #print("inputs,enc_output,enc_hidden,dec_hidden",inputs.get_shape(),enc_output.get_shape(),enc_hidden.get_shape(),dec_hidden.get_shape())
    # dec_input = tf.expand_dims([vocab.word_to_id(vocab.START_DECODING)] * batch_size, 1)
    dec_input = tf.constant([2] * batch_size)
    dec_input = tf.expand_dims(dec_input, axis=1)#shape=(batch_size,1)
    
    context_vector, _ = model.attention(dec_hidden, enc_output)#shape=(batch_size,enc_hidden_size)
    # print("dec_inp,contexy_vector",dec_input.get_shape(),context_vector.get_shape())
    #核心套路：一批数据先进来统一做一个encoder,在统一做一个attention得到一个初始的context_vector，再对每一批数据的一个个字符进行decoder、attention,得到那一批数据在同一位置的decoder输出值
    for t in range(params['max_dec_len']):
        # 单步预测
        #通过调用decoder得到预测的概率分布,参考训练阶段
        _, pred, dec_hidden = model.decoder(dec_input,
                                            dec_hidden,
                                            enc_output,
                                            context_vector)
        
        context_vector, _ = model.attention(dec_hidden, enc_output)
        #通过调用tf.argmax完成greedy search，得到predicted_ids，找到概率值最大的作为预测的输出
        predicted_ids = tf.argmax(pred,axis=1).numpy()
        #将每一步的预测结果都保存起来
        for index, predicted_id in enumerate(predicted_ids):
            
            #print("predicted_id=",predicted_id)#不要瞎打印，不然整个计算过程会很慢
            predicts[index] += vocab.id_to_word(predicted_id) + ' '#此处注意设置字典大小最好为实际大小，不然会提示找不到对应的词
        
        # 前一步的预测结果作为下一步的输入
        dec_input = tf.expand_dims(predicted_ids, 1)

    results = []
    for predict in predicts:
        # 去掉句子前后空格
        predict = predict.strip()
        # 句子小于max len就结束了 截断vocab.word_to_id('[STOP]')
        if '[STOP]' in predict:
            # 截断stop
            predict = predict[:predict.index('[STOP]')]
        # 保存结果
        results.append(predict)
    return results

def beam_decode(model, dataset, vocab, params):
    # 存储结果
    batch_size = params["batch_size"]
    results = []
    sample_size = TEST_NUM_SAMPLES #20000(小样本数50)
    # batch 操作轮数 math.ceil向上取整 小数 +1
    # 因为最后一个batch可能不足一个batch size 大小 ,但是依然需要计算
    steps_epoch = sample_size // batch_size + 1
    
    #此处可显示进度
    for i in tqdm(range(steps_epoch)):
        enc_data, _ = next(iter(dataset))#数据一批批送入
        results += batch_beam_decode(model, enc_data, vocab, params)
    return results

class Hypothesis:
    """ Class designed to hold hypothesises throughout the beamSearch decoding """

    def __init__(self, tokens, log_probs, state):
        # list of all the tokens from time 0 to the current time step t
        self.tokens = tokens
        # list of the log probabilities of the tokens of the tokens
        self.log_probs = log_probs
        # decoder state after the last token decoding
        self.state = state

    def extend(self, token, log_prob, state):
        """Method to extend the current hypothesis by adding the next decoded token and all
        the informations associated with it"""
        return Hypothesis(tokens=self.tokens + [token],  # we add the decoded token
                          log_probs=self.log_probs + [log_prob],  # we add the log prob of the decoded token
                          state=state,  # we update the state
                          )

    @property
    def latest_token(self):
        return self.tokens[-1]

    @property
    def tot_log_prob(self):
        return sum(self.log_probs)

    @property
    def avg_log_prob(self):
        return self.tot_log_prob / len(self.tokens)


def batch_beam_decode(model, enc_data, vocab, params):
    #去掉部分参数，无用参数enc_pad_mask,只保留有用参数 enc_inp, enc_outputs, dec_input, dec_state,
    def decode_onestep(enc_inp, enc_outputs, dec_input, dec_state):
        """
            Method to decode the output step by step (used for beamSearch decoding)
            Args:
                sess : tf.Session object
                batch : current batch, shape = [beam_size, 1, vocab_size( + max_oov_len if pointer_gen)]
                (for the beam search decoding, batch_size = beam_size)
                enc_outputs : hiddens outputs computed by the encoder LSTM
                dec_state : beam_size-many list of decoder previous state, LSTMStateTuple objects,
                shape = [beam_size, 2, hidden_size]
                dec_input : decoder_input, the previous decoded batch_size-many words, shape = [beam_size, embed_size]
                cov_vec : beam_size-many list of previous coverage vector
            Returns: A dictionary of the results of all the ops computations (see below for more details)
        """
        #此处需要让batch_size=beam_size,可以用GPU加速，矩阵变换30000&9变成90000*3，做并行计算
        final_dists, dec_hidden, attentions, p_gens = model(enc_outputs,  # shape=(3, 115, 256)
                                                            dec_state,  # shape=(3, 256)
                                                            enc_inp,  # shape=(3, 115)
                                                            dec_input)  # shape=(3, 1)
        #5000*2变成5000x2*1
        #拿到最大的概率值和对应的token_id,再将概率值进行log计算
        top_k_probs, top_k_ids = tf.nn.top_k(tf.squeeze(final_dists), k=params["beam_size"] * 2)
        top_k_log_probs = tf.math.log(top_k_probs)

        results = {"dec_state": dec_hidden,
                   "top_k_ids": top_k_ids,
                   "top_k_log_probs": top_k_log_probs,
                   }
        return results

        # 判断输入长度
    
    batch_data = enc_data["enc_input"]#shape=(batch_size,实际输入序列长度)
    batch_size = enc_data["enc_input"].shape[0]
    # 开辟结果存储list
    predicts = [''] * batch_size#也是一批一起运算，大小为batch_size
    #print("batch_data,batch_size,predicts",batch_data,batch_size,predicts)
    # inputs = batch_data # shape=(batch_size,实际序列长度)
    inputs = tf.convert_to_tensor(batch_data)#
    # We run the encoder once and then we use the results to decode each time step token
    
    enc_outputs, state = model.call_encoder(inputs)
    hyps = [Hypothesis(tokens=[vocab.word_to_id('[START]')],
                       log_probs=[0.0],
                       state=state[0]) for _ in range(params['batch_size'])]
    # print('hyps', hyps)
    results = []  # list to hold the top beam_size hypothesises
    steps = 0  # initial step

    while steps < params['max_dec_steps']:
        # print('step is ', steps)
        latest_tokens = [h.latest_token for h in hyps]  # latest token for each hypothesis , shape : [beam_size]
        latest_tokens = [t if t in range(params['vocab_size']) else vocab.word_to_id('[UNK]') for t in latest_tokens]
        
        tokens = [h.tokens for h in hyps]  # tokens for each hypothesis , shape : [beam_size]
        tokens = [t if t in range(params['vocab_size']) else vocab.word_to_id('[UNK]') for t in tokens]
        
        states = [h.state for h in hyps]
        # we decode the top likely 2 x beam_size tokens tokens at time step t for each hypothesis
        dec_input = tf.expand_dims(latest_tokens, axis=1)  # shape=(3, 1)

        enc_input = tf.expand_dims(tokens, axis=1)  # shape=(3, 1)

        dec_states = tf.stack(states, axis=0)
        print('decode_onestep',enc_input.get_shape(),enc_outputs.get_shape(),dec_input.get_shape(),dec_states.get_shape())
        returns = decode_onestep(enc_input,  # shape=(3, 115)
                                 enc_outputs,  # shape=(3, 115, 256)
                                 dec_input,  # shape=(3, 1)
                                 dec_states)  # shape=(3, 256)
        #可以修改：topk_ids, prediction, new_states
        topk_ids, topk_log_probs, new_states = returns['top_k_ids'],\
                                                returns['top_k_log_probs'],\
                                                returns['dec_state'],\

        # print('topk_ids is ', topk_ids)
        # print('topk_log_probs is ', topk_log_probs)
        all_hyps = []
        num_orig_hyps = 1 if steps == 0 else len(hyps)
        num = 1
        # print('num_orig_hyps is ', num_orig_hyps)
        for i in range(num_orig_hyps):
            h, new_state = hyps[i], new_states[i]
            num += 1
            # print('num is ', num)
            for j in range(params['beam_size'] * 2):
                # we extend each hypothesis with each of the top k tokens
                # (this gives 2 x beam_size new hypothesises for each of the beam_size old hypothesises)
                new_hyp = h.extend(token=topk_ids[i, j].numpy(),
                                   log_prob=topk_log_probs[i, j],
                                   state=new_state,
                                   )
                all_hyps.append(new_hyp)
        # in the following lines, we sort all the hypothesises, and select only the beam_size most likely hypothesises
        hyps = []
        sorted_hyps = sorted(all_hyps, key=lambda h: h.avg_log_prob, reverse=True)
        for h in sorted_hyps:
            if h.latest_token == vocab.word_to_id('[STOP]'):
                if steps >= params['min_dec_steps']:
                    results.append(h)
            else:
                # print(h.latest_token)
                hyps.append(h)
            if len(hyps) == params['beam_size'] or len(results) == params['beam_size']:
                break
        # print('hyps is ', hyps.)
        # print('steps is ', steps)
        steps += 1

    if len(results) == 0:
        results = hyps

    # At the end of the loop we return the most likely hypothesis, which holds the most likely ouput sequence,
    # given the input fed to the model
    hyps_sorted = sorted(results, key=lambda h: h.avg_log_prob, reverse=True)
    best_hyp = hyps_sorted[0]
    # print('best_hyp.tokens is ', best_hyp.tokens)
    best_hyp.abstract = " ".join(output_to_words(best_hyp.tokens, vocab, batch[0]["article_oovs"][0])[1:-1])
    best_hyp.text = batch[0]["article"].numpy()[0].decode()
    print('best_hyp is ', best_hyp.abstract)
    return best_hyp

import transformers
from transformers import AutoTokenizer
import torch
import numpy as np
import re
from tqdm import tqdm
from flashrag.evaluator import Evaluator
from flashrag.utils import get_retriever, get_generator
from flashrag.pipeline import BasicPipeline


class IterativePipeline(BasicPipeline):
    def __init__(self, config, iter_num = 3):
        super().__init__(config)
        self.iter_num = iter_num
        self.retriever = get_retriever(config)
        self.generator = get_generator(config)
    
    def run(self, dataset, do_eval=True, pred_process_fun=None):
        questions = dataset.question

        # run in batch
        past_generation_result = [] # list of N items
        for iter_idx in range(self.iter_num):
            if iter_idx == 0:
                input_query = questions
            else:
                assert len(questions) == len(past_generation_result)
                input_query = [f"{q} {r}" for q,r in zip(questions, past_generation_result)]
            
            # generation-augmented retrieval
            retrieval_results = self.retriever.batch_search(input_query)
            dataset.update_output(f'retrieval_result_iter_{iter_idx}', retrieval_results)
            
            # retrieval-augmented generation
            input_prompts = self.build_prompt(questions, retrieval_results)
            dataset.update_output(f'prompt_iter_{iter_idx}', input_prompts)
            past_generation_result = self.generator.generate(input_prompts)
            dataset.update_output(f'pred_iter_{iter_idx}', past_generation_result)
        
        dataset.update_output("pred", past_generation_result)
        dataset = self.evaluate(dataset, do_eval=do_eval, pred_process_fun=pred_process_fun)

        return dataset

class SelfRAGPipeline(BasicPipeline):
    rel_tokens_names = ["[Irrelevant]", "[Relevant]"]
    retrieval_tokens_names = ["[No Retrieval]",
                            "[Retrieval]", "[Continue to Use Evidence]"]
    utility_tokens_names = ["[Utility:1]", "[Utility:2]",
                            "[Utility:3]", "[Utility:4]", "[Utility:5]"]
    ground_tokens_names = ["[Fully supported]",
                        "[Partially supported]", "[No support / Contradictory]"]
    other_special_tokens = ["<s>", "</s>", "[PAD]",
                            "<unk>", "<paragraph>", "</paragraph>"]
    control_tokens = ["[Fully supported]", "[Partially supported]", "[No support / Contradictory]", "[No Retrieval]", "[Retrieval]",
                    "[Irrelevant]", "[Relevant]", "<paragraph>", "</paragraph>", "[Utility:1]", "[Utility:2]", "[Utility:3]", "[Utility:4]", "[Utility:5]"]
    
    task_inst = {"wow": "Given a chat history separated by new lines, generates an informative, knowledgeable and engaging response. ",
             "fever": "Is the following statement correct or not? Say true if it's correct; otherwise say false.",
             "eli5": "Provide a paragraph-length response using simple words to answer the following question.",
             "obqa": "Given four answer candidates, A, B, C and D, choose the best answer choice.",
             "arc_easy": "Given four answer candidates, A, B, C and D, choose the best answer choice.",
             "arc_c": "Given four answer candidates, A, B, C and D, choose the best answer choice.",
             "trex": "Given the input format 'Subject Entity [SEP] Relationship Type,' predict the target entity.",
             "asqa": "Answer the following question. The question may be ambiguous and have multiple correct answers, and in that case, you have to provide a long-form answer including all correct answers."}


    def __init__(self, config, threhsold=0.2, max_depth=2, beam_width=2,  
                 w_rel=1.0, w_sup=1.0, w_use=1.0,
                 use_grounding=True, use_utility=True, use_seqscore=True, ignore_cont=True,
                 mode='adaptive_retrieval'):
        
        super().__init__(config)
        self.retriever = get_retriever(config)
        self.generator = get_generator(config)

        assert mode in ['adaptive_retrieval', 'always_retrieve', 'no_retrieval']

        self.task = self.load_task(config['dataset_name'])
        self.threshold = threhsold
        self.max_depth = max_depth
        self.beam_width = beam_width
        self.w_rel, self.w_sup, self.w_use = w_rel, w_sup, w_use
        self.use_grounding = use_grounding
        self.use_utility = use_utility
        self.use_seqscore = use_seqscore
        self.ignore_cont = ignore_cont
        self.mode = mode
        self.closed = self.task in ['fever','arc_c']
        tokenizer = AutoTokenizer.from_pretrained(config['generator_model_path'], padding_side="left")
        self.ret_tokens, self.rel_tokens, self.grd_tokens, self.ut_tokens = self.load_special_tokens(
            tokenizer, use_grounding = use_grounding, use_utility = use_utility)
        
    
    def load_special_token(self, tokenizer, use_grounding, use_utility):
        ret_tokens = {token: tokenizer.convert_tokens_to_ids(
            token) for token in self.retrieval_tokens_names}
        rel_tokens = {}
        for token in ["[Irrelevant]", "[Relevant]"]:
            rel_tokens[token] = tokenizer.convert_tokens_to_ids(token)

        grd_tokens = None
        if use_grounding is True:
            grd_tokens = {}
            for token in self.ground_tokens_names:
                grd_tokens[token] = tokenizer.convert_tokens_to_ids(token)

        ut_tokens = None
        if use_utility is True:
            ut_tokens = {}
            for token in self.utility_tokens_names:
                ut_tokens[token] = tokenizer.convert_tokens_to_ids(token)

        return ret_tokens, rel_tokens, grd_tokens, ut_tokens
    
    def build_prompt(self, questions):
        # TODO: add support for more task
        # TODO: add support for more type of prompts
        task_instruction = self.task_inst.get(self.task, None)
        question_insts = [f"{task_instruction}\n\n## Input:\n\n{question}" if task_instruction is not None \
                    else question for question in questions]
        input_prompts = [f"### Instruction:\n{q_inst}\n\n### Response:\n" for q_inst in question_insts]
        
        return input_prompts

    def judge_retrieve(self, input_prompts):
        """Calculate whether a retrieve is required based on the output probability of 
        the special token in the model"""

        if self.mode != "always_retrieve":
            # result for total batch
            all_pred_token_ids = []
            all_pred_text = []
            all_pred_log_probs = []
            preds = self.generator.generate(input_prompts, return_raw_output=True, logprobs=32000)
            for single_pred in preds:
                pred_token_ids = single_pred.outputs[0].token_ids
                pred_text = single_pred.outputs[0].text
                pred_log_probs = single_pred.outputs[0].logprobs
                all_pred_token_ids.append(pred_token_ids)
                all_pred_text.append(pred_text)
                all_pred_log_probs.append(pred_log_probs)

        if self.mode == "always_retrieve":
            retrieval_flags = [True] * len(input_prompts)

        elif self.mode == "no_retrieval":
            retrieval_flags = [False] * len(input_prompts)

        else:
            retrieval_flags = []
            for idx, single_pred in enumerate(preds):
                if self.threshold is not None:
                    score_dict = {}
                    for tok, id in self.ret_tokens.items():
                        if id not in all_pred_log_probs[idx][0][id]:
                            score_dict[tok] = -100
                        prob = all_pred_log_probs[idx][0][id]
                        score_dict[tok] = float(prob)
                    do_retrieve = score_dict["[Retrieval]"] / (
                        score_dict["[Retrieval]"] + score_dict["[No Retrieval]"]) > self.threshold
                else:
                    do_retrieve = "[Retrieval]" in all_pred_text[idx]
                
                retrieval_flags.append(do_retrieve)

        return retrieval_flags

    def critic_preds(self, preds):
        """Evaluate predictions using different retrieval docs"""

        relevance_score_dict = {}
        grd_score_dict = {}
        ut_score_dict = {}
        overall_scores = {}
        results = {}
        for p_idx, pred in enumerate(preds):
            pred_token_ids = pred.outputs[0].token_ids
            pred_text = pred.outputs[0].text
            pred_log_probs = pred.outputs[0].logprobs
            seq_score = pred.outputs[0].cumulative_logprob / \
                max(len(pred.outputs[0].token_ids), 1)

            relevance_score_dict.setdefault(p_idx, {})
            grd_score_dict.setdefault(p_idx, {})
            ut_score_dict.setdefault(p_idx, {})
            # Compute reward scores
            for tok, id in self.rel_tokens.items():
                prob = pred_log_probs[0][id] if id in pred_log_probs[0] else -100
                relevance_score_dict[p_idx][tok] = np.exp(float(prob))

            if self.grd_tokens is not None:
                groundness_token_appear_indices = []
                for tok_idx, tok in enumerate(pred_token_ids):
                    if tok in list(self.grd_tokens.values()):
                        groundness_token_appear_indices.append(tok_idx)
                        break
                if len(groundness_token_appear_indices) > 0:
                    idx = groundness_token_appear_indices[0]
                    for token, token_id in self.grd_tokens.items():
                        prob = pred_log_probs[idx][token_id] if token_id in pred_log_probs[idx] else -100
                        grd_score_dict[p_idx][token] = np.exp(float(prob))
            utility_token_appear_indices = []
            if self.ut_tokens is not None:
                for tok_idx, tok in enumerate(pred_token_ids):
                    if tok in list(self.ut_tokens.values()):
                        utility_token_appear_indices.append(tok_idx)
                if len(utility_token_appear_indices) > 0:
                    idx = utility_token_appear_indices[0]
                    for token, token_id in self.ut_tokens.items():
                        prob = pred_log_probs[idx][token_id] if token_id in pred_log_probs[idx] else -100
                        ut_score_dict[p_idx][token] = np.exp(float(prob))

            relevance_score = relevance_score_dict[p_idx]["[Relevant]"] / (
                np.sum(list(relevance_score_dict[p_idx].values())))

            if len(grd_score_dict[p_idx]) == 3:
                gt_sum = np.sum(list(grd_score_dict[p_idx].values()))
                ground_score = (grd_score_dict[p_idx]["[Fully supported]"] / gt_sum) + 0.5 * (
                    grd_score_dict[p_idx]["[Partially supported]"] / gt_sum)
            else:
                ground_score = 0.0

            if len(ut_score_dict[p_idx]) == 5:
                ut_sum = np.sum(list(ut_score_dict[p_idx].values()))
                ut_scores = [-1, -0.5, 0, 0.5, 1]
                utility_score = np.sum(
                    [ut_scores[i] * (ut_score_dict[p_idx]["[Utility:{}]".format(i+1)] / ut_sum) for i in range(len(ut_scores))])
            else:
                utility_score = 0.0

            if self.use_seqscore is True:
                final_score = np.exp(seq_score) + self.w_rel * relevance_score + \
                    self.w_sup * ground_score + self.self.w_use * utility_score
            else:
                final_score = self.w_rel * relevance_score + \
                    self.w_sup * ground_score + self.w_use * utility_score

            overall_scores[p_idx] = {"final_score": final_score,
                                     "relevance_score": relevance_score,
                                     "ground_score": ground_score,
                                     "utility_score": utility_score,
                                     "relevance_score_dict": relevance_score_dict,
                                     "grd_score_dict": grd_score_dict,
                                     "ut_score_dict": utility_score}
            results["retrieval_{}".format(p_idx)] = {
                "pred": pred_text, "score": final_score}
        
        # modify and add do retrieve tokens (only used in long-form generation)
        final_preds =[]
        if "[No Retrieval]" in pred_text:
            ret_token_appear_indices = []
            substrings = pred_text.split("[No Retrieval]")

            for tok_idx, tok in enumerate(pred_token_ids):
                if tok == self.ret_tokens["[No Retrieval]"]:
                    ret_token_appear_indices.append(tok_idx)
                    substrings
                    print("retrieval_tokens")

            ret_token_score_dict = {}
            retrieval_remap = {}
            for order, idx in enumerate(ret_token_appear_indices):
                ret_token_score_dict.setdefault(order, {})
                for tok, tok_id in self.ret_tokens.items():
                    prob = pred_log_probs[idx][tok_id] if tok_id in pred_log_probs[idx] else -100
                    ret_token_score_dict[order][tok] = np.exp(prob)
                if ret_token_score_dict[order]["[Retrieval]"] + ret_token_score_dict[order]["[No Retrieval]"] != 0.0:
                    do_retrieve = (ret_token_score_dict[order]["[Retrieval]"] + ret_token_score_dict[order]["[Continue to Use Evidence]"]) / (
                        ret_token_score_dict[order]["[Retrieval]"] + ret_token_score_dict[order]["[No Retrieval]"]) > self.threshold
                else:
                    do_retrieve = 0.0
                if do_retrieve > self.threshold:
                    retrieval_remap[order] = True
                else:
                    retrieval_remap[order] = False
            processed_pred = ""
            for substr_i, substring in enumerate(substrings):
                if substr_i in retrieval_remap and retrieval_remap[substr_i] is True:
                    processed_pred += substring + "[Retrieval]"
                else:
                    processed_pred += substring + "[No Retrieval]"
            pred_text = processed_pred
            final_preds.append(pred_text)
        else:
            final_preds.append(pred_text)

        scores = [overall_scores[p_idx]["final_score"] for p_idx in overall_scores]

        return results, final_preds, scores, overall_scores


    def postprocess_prediction(self, pred):
        def fix_spacing(input_text):
            import re
            # Add a space after periods that lack whitespace
            output_text = re.sub(r'(?<=\w)([.!?])(?=\w)', r'\1 ', input_text)
            return output_text
        
        for token in self.control_tokens:
            pred = pred.replace(token, "")
        if "</s>" in pred:
            pred = pred.replace("</s>", "")
        if "\n" in pred:
            pred = pred.replace("\n", "")
        if "<|endoftext|>" in pred:
            pred = pred.replace("<|endoftext|>", "")

        pred = pred.strip()
        if type(pred) is str and pred[0] == "#" or pred[0] == ":":
            pred = pred[1:]
        if len(pred) == 0:

            return ""
        
        return fix_spacing(pred)
    

    def select_best_prediction(self, results):
        answer2score = {}
        if self.closed is True:
            for key, result in results.items():
                answer = self.postprocess_prediction(result["pred"])
                score = result["score"]
                answer2score.setdefault(answer, 0)
                answer2score[answer] += score
            sorted_answers = sorted(
                answer2score.items(), key=lambda x: x[1], reverse=True)
            best_pred = sorted_answers[0][0]
        else:
            path2score = {key: item["score"] for key,
                          item in results.items() if key != "no_retrieval"}
            best_path = sorted(path2score.items(),
                               key=lambda x: x[1], reverse=True)[0][0]
            best_pred = results[best_path]["pred"]
        
        return best_pred

    def run_single_beam(self, prompt, item_retrieval_result=None):
        curr_depth = 1
        terminated = False
        node_id = 0
        prediction_tree = {}
        levels = {}
        prediction_tree[node_id] = {"prompt": prompt, "pred": "[Retrieval]",
                                    "processed_pred": "", "score": None, "ctx": None, "parent": None}
        levels[0] = [0]
        while curr_depth < self.max_depth:
            levels[curr_depth] = []
            if curr_depth-1 in levels and terminated is False:
                for node in levels[curr_depth-1]:
                    pred = prediction_tree[node]["pred"]
                    if pred == "</s>":
                        terminated = True
                        continue
                    prompt = prediction_tree[node]["prompt"]
                    prev_generation = prediction_tree[node]["processed_pred"]
                    score = prediction_tree[node]["score"]
                    if "[Retrieval]" in pred:
                        retrieval_results = {}

                        if item_retrieval_result is not None:
                            aug_prompts = [prompt + prev_generation + "[Retrieval]" + "<paragraph>{}</paragraph>".format(
                                para['contents']) for para in item_retrieval_result]
                        else:
                            aug_prompts = [prompt + prev_generation]

                        item_pred = self.generator.generate(aug_prompts, return_raw_output=True)
                        _, preds, scores, overall_score_dict = self.critic_preds(item_pred)

                        for i, (pred, p_score) in enumerate(zip(preds, scores)):
                            retrieval_results[i] = {
                                "pred": pred, "score": p_score}

                        for i, result in retrieval_results.items():
                            node_id += 1
                            node_score = result["score"] * \
                                score if score is not None else result["score"]
                            pred = result["pred"]
                            prediction_tree[node_id] = {"prompt": prompt + prev_generation, "pred": pred,
                                                        "score": node_score, "ctx": item_retrieval_result[i], "parent": node,
                                                        "overall_score_dict": overall_score_dict}

                            if "[Retrieval]" in pred:
                                gen_result_index = pred.index("[Retrieval]")
                                prev_generation = pred[:gen_result_index]
                            else:
                                prev_generation = pred
                            prediction_tree[node_id]["processed_pred"] = prev_generation
                            levels[curr_depth].append(node_id)

                current_rank = levels[curr_depth]
                node2score = {
                    node_id: prediction_tree[node_id]["score"] for node_id in current_rank}
                top_nodes = sorted(node2score.items(), key=lambda x: x[1], reverse=True)[
                    :self.beam_width]
                levels[curr_depth] = [node[0] for node in top_nodes]
                curr_depth += 1
            else:
                break

        final_prediction = ""
        parent = 0
        best_selections = {}

        # Traverse from the bottom
        levels = {k: v for k, v in levels.items() if len(v) > 0 and k != 0}
        for path_i, node in enumerate(levels[len(levels)]):
            if node == 0:
                break
            best_selections[path_i] = [node]
            current_node = node
            current_level = curr_depth
            if current_node is None:
                continue
            while current_level > 0 and current_node is not None:
                parent = prediction_tree[current_node]["parent"]
                best_selections[path_i] = [parent] + best_selections[path_i]
                current_node = parent
                current_level += 1

        final_prediction = {}
        splitted_sentences = {}
        original_splitted_sentences = {}
        ctxs = {}
        for path_i, nodes in best_selections.items():
            final_prediction[path_i] = " ".join([prediction_tree[node]["processed_pred"] for node in nodes if node is not None and (
                self.ignore_cont is False or (self.ignore_cont is True and "[No support / Contradictory]" not in prediction_tree[node]["processed_pred"]))])
            splitted_sentences[path_i] = [prediction_tree[node]["processed_pred"] for node in nodes if node is not None and (
                self.ignore_cont is False or (self.ignore_cont is True and "[No support / Contradictory]" not in prediction_tree[node]["processed_pred"]))]
            original_splitted_sentences[path_i] = [prediction_tree[node]["pred"] for node in nodes if node is not None and (
                self.ignore_cont is False or (self.ignore_cont is True and "[No support / Contradictory]" not in prediction_tree[node]["processed_pred"]))]
            ctxs[path_i] = [prediction_tree[node]["ctx"] for node in nodes if node is not None and (self.ignore_cont is False or (
                self.ignore_cont is True and "[No support / Contradictory]" not in prediction_tree[node]["processed_pred"]))]

        result = {"final_prediction": final_prediction,
                "splitted_sentences": splitted_sentences,
                "original_splitted_sentences": original_splitted_sentences,
                "best_selections": best_selections,
                "ctxs": ctxs,
                "prediction_tree": prediction_tree}
        
        return final_prediction[0], result

    def postprocess_long_form(self, pred, intermediate):
        final_output = ""
        docs = []
        prev_gen = []
        if "splitted_sentences" not in intermediate:
            final_output = self.postprocess_prediction(pred)
        else:
            if len(self.postprocess(pred)) == 0:
                intermediate["splitted_sentences"][0], intermediate["ctxs"][
                    0] = intermediate["splitted_sentences"][1], intermediate["ctxs"][1]
            for idx, (sent, doc) in enumerate(zip(intermediate["splitted_sentences"][0], intermediate["ctxs"][0])):
                if len(sent) == 0:
                    continue
                postprocessed_result = self.postprocess_prediction(sent)
                if postprocessed_result in prev_gen:
                    continue
                else:
                    prev_gen.append(postprocessed_result)
                final_output += postprocessed_result[:-1] + " [{}]".format(idx) + ". "
                docs.append(doc)
            if len(final_output) == 0:
                final_output = final_output
            if len(final_output) > 0 and final_output[-1] == " ":
                final_output = final_output[:-1]
            final_output = final_output.strip()
            final_output = final_output.replace(
                ".[Continue to Use Evidence]", " [1]. ")
            final_output = final_output.replace(". [1] ", " [1]. ")

        return final_output


    def long_form_run(self, dataset, do_eval=True, pred_process_fun=None):
        questions = dataset.question
        retrieval_results = self.retriever.batch_search(questions)
        dataset.update_output('retrieval_result', retrieval_results)

        input_prompts = self.build_prompt(questions)

        # determine whether to retrieve
        retrieval_flags = self.judge_retrieve(input_prompts)
        dataset.update_output('retrieval_flag', retrieval_flags)

        # for long form task, only support single item run
        for item, prompt, retrieval_flag in zip(dataset, input_prompts, retrieval_flags):
            if retrieval_flag:
                pred, intermediate_result = self.run_single_beam(prompt, item_retrieval_result=item.retrieval_result)
                item.update_output('intermediate_result', intermediate_result)

                if self.task == "factscore":
                    pred = self.postprocess_prediction(pred)
                else:
                    assert self.task in ['asqa','eli5']
                    pred = self.postprocess_long_form(pred, intermediate_result)
            else:
                prompt += "[No Retrieval]"
                pred = self.generator.generate(prompt)[0]

            item.update_output('pred', pred)

        dataset = self.evaluate(dataset, do_eval=do_eval, pred_process_fun=pred_process_fun)

        return dataset
            



    def run(self, dataset, do_eval=True, pred_process_fun=None):
        questions = dataset.question
        retrieval_results = self.retriever.batch_search(questions)
        dataset.update_output('retrieval_result', retrieval_results)

        input_prompts = self.build_prompt(questions)

        # determine whether to retrieve
        retrieval_flags = self.judge_retrieve(input_prompts)
        dataset.update_output('retrieval_flag', retrieval_flags)

        # process input item based on whether to retrieve
        batch_input_list = []
        for idx, (prompt,item) in enumerate(zip(input_prompts,dataset)):
            retrieval_flag = retrieval_flags[idx]

            if retrieval_flag:
                retrieval_result = retrieval_results[idx]
                # for each doc in retrieval result, there is a prompt as input
                prompt_list = [prompt + "[Retrieval]<paragraph>{}</paragraph>".format(para['contents']) \
                            for para in retrieval_result]
            else:
                prompt += "[No Retrieval]"
                prompt_list = [prompt]

            item.update_output('prompt', prompt_list)
            batch_input_list += prompt_list
        
        batch_pred = self.generator.generate(batch_input_list, return_raw_output=True, logprobs=5000)

        # parse output based on retrieval flag
        pred_idx = 0
        pred_answer_list = []
        for idx, (retrieval_flag,item) in enumerate(zip(retrieval_flags, dataset)):
            if retrieval_flag:
                # for item that need retrieval, there may have more than one prediction
                item_pred = batch_pred[pred_idx:pred_idx+len(retrieval_results[idx])]
                pred_idx += len(retrieval_results[idx])
                critic_result,_,_,_ = self.critic_preds(item_pred)
                item.update_output('critic_result', critic_result)

                # select best prediction
                pred = self.select_best_prediction(critic_result)

            else:
                item_pred = batch_pred[pred_idx:pred_idx+1][0]
                pred_idx += 1
                pred = item_pred.outputs[0].text

            pred = self.postprocess_prediction(pred)
            pred_answer_list.append(pred)
        
        dataset.update_output("pred",pred_answer_list)

        dataset = self.evaluate(dataset, do_eval=do_eval, pred_process_fun=pred_process_fun)

        return dataset
        
        
        
class FLAREPipeline(BasicPipeline):
    def __init__(self, config, threshold=0.2, look_ahead_steps=64, max_generation_length=256, max_iter_num=5):
        super().__init__(config)
        # from nltk.tokenize.punkt import PunktSentenceTokenizer
        # self.sentence_spliter = PunktSentenceTokenizer()

        self.retriever = get_retriever(config)
        self.generator = get_generator(config)
        self.threshold = threshold
        self.max_generation_length = max_generation_length
        self.max_iter_num = max_iter_num
        self.look_ahead_steps = look_ahead_steps
        self.stop_sym = '!@#$%^&*()\n\n)(*&^%$#@!'

    def get_next_sentence(self, output, scores):
        import re
        tokenizer = self.generator.tokenizer
        text_sentences = re.split(r'(?<=[^A-Z].[.?]) +', output)
        token_id_sentences = [tokenizer.encode(s, add_special_tokens=False) for s in text_sentences]
        output_ids = tokenizer.encode(output, add_special_tokens=False)
        assert sum([len(s) for s in token_id_sentences]) == len(
            output_ids), f"token id sentences length {sum([len(s) for s in token_id_sentences])} not equal to output ids length {len(target_ids)}\n{token_id_sentences}\n{text_sentences}\n{answer}"
        
        first_sent_ids = token_id_sentences[0]
        first_sent_score = scores[:len(first_sent_ids)]
        return text_sentences[0], first_sent_score

    def judge_sent_confidence(self, sent, sent_score):
        judge_result = all([score > self.threshold for score in sent_score])
        new_query = None
        if not judge_result:
            tokenizer = self.generator.tokenizer
            sent_ids = tokenizer.encode(sent, add_special_tokens=False)
            assert len(sent_ids) == len(sent_score)
            new_query_ids = [i for i,score in zip(sent_ids,sent_score) if score > self.threshold]
            new_query = tokenizer.decode(new_query_ids)
        return judge_result, new_query
        
    def run_item(self, item):
        question = item.question
        gen_length = 0
        iter_round = 0
        final_gen_result = ""
        while gen_length < self.max_generation_length and iter_round < self.iter_round:
            input_prompt = self.build_prompt(
                question_list=[question], use_reference=False, previous_gen=final_gen_result)[0]
            # scores: token logits of the whole generation seq
            round_gen_output, scores = self.generator.generate(
                input_prompt, return_scores=True, stop=self.stop_sym, max_new_tokens=self.look_ahead_steps)
            round_gen_output, scores = round_gen_output[0], scores[0]
            # next_sent_scores: token logits of the first sent in generation seq
            next_sent, next_sent_score = self.get_next_sentence(round_gen_output, scores)
            # judge next sentence
            judge_result, query = self.judge_sent_confidence(next_sent, next_sent_score)
            
            if not judge_result:
                # do retrieval-augmented generation
                retrieval_result = self.retriever.search(query)
                item.update_output('retrieval_result', retrieval_result)
                
                input_prompt = self.build_prompt(
                    question_list = [question], 
                    retrieval_results = retrieval_result, 
                    previous_gen = final_gen_result)[0]
                output, scores = self.generator.generate(
                    input_prompt, return_scores=True, stop=self.stop_sym, max_new_tokens=self.look_ahead_steps)
                output, scores = output[0], scores[0]
                next_sent, _ = self.get_next_sentence(output, scores)

            final_gen_result += next_sent
            gen_length += len(next_sent_score)   
            iter_round += 1

        # TODO: save intermediate result
        item.update_output('pred', final_gen_result)
                     

    def run(self, dataset, do_eval=True, pred_process_fun=None):
        for item in tqdm(dataset, desc="Inference: "):
            self.run_item(item)

        dataset = self.evaluate(dataset, do_eval=do_eval, pred_process_fun=pred_process_fun)
        return dataset
            

class SelfAskPipeline(BasicPipeline):
    from flashrag.utils import SELF_ASK_PROMPT
    P_INS = SELF_ASK_PROMPT
    FOLLOW_UP_PATTERN = r"Follow up:.*\n"

    def __init__(self, config, max_iter=5, single_hop=True):
        super().__init__(config)
        self.retriever = get_retriever(config)
        self.generator = get_generator(config)

        self.single_hop = single_hop
        self.max_iter = max_iter
    
    def format_reference(self, retrieval_result):
        format_reference = ''
        for idx, doc_item in enumerate(retrieval_result):
            content = doc_item['contents']
            title = content.split("\n")[0]
            text = "\n".join(content.split("\n")[1:])
            format_reference += f"Context{idx+1}: {text}\n"

        return format_reference

    def _remove_duplicate_doc(self, docs):
        assert all(['id' in doc for doc in docs])
        new_doc_list = []
        exist_ids = []
        for doc in docs:
            doc_id = doc['id']
            if doc_id not in exist_ids:
                exist_ids.append(doc_id)
                new_doc_list.append(doc)
        return new_doc_list

    def run_item(self, item):
        question = item.question
        retrieval_result = self.retriever.search(question)

        stop_condition = "Intermediate answer:"
        follow_ups = "No." if self.single_hop else "Yes."
        res = ""
        early_exit = False
        for idx in range(self.max_iter):
            input_prompt = (
                self.P_INS
                + "\n"
                + self.format_reference(retrieval_result)
                + f"\nQuesiton: {question}"
                + "\nAre follow up questions needed here: "
                + follow_ups 
                + "\n"
                + res
            )
            gen_out = self.generator.generate(input_prompt, stop=["Context:", "#"])[0]
            item.update_output(f'intermediate_output_iter{idx}', gen_out)
            
            if stop_condition == "Intermediate answer:":
                res += gen_out.split("Intermediate answer:")[0]
                stop_condition = "Follow up:"

            elif stop_condition == "Follow up:":
                followup_split = re.split(self.FOLLOW_UP_PATTERN, gen_out)
                res += followup_split[0]

                if len(followup_split) > 1:
                    res += re.findall(self.FOLLOW_UP_PATTERN, gen_out)[0]

            # make sure the result does not end in a new line
            if len(res) == 0:
                early_exit = True
                break
            if res[-1] == "\n":
                res = res[:-1]

            if "Follow up: " in gen_out:
                # get the first follow up
                new_query = [l for l in gen_out.split("\n") if "Follow up: " in l][
                    0
                ].split("Follow up: ")[-1]
                new_retrieval_result = self.retriever.search(new_query)
                retrieval_result.append(new_retrieval_result)
                retrieval_result = self._remove_duplicate_doc(retrieval_result)

            elif "So the final answer is: " in gen_out:
                res = (
                    self.format_reference(retrieval_result)
                    + f"\nQuesiton: {question}"
                    + "\nAre follow up questions needed here: "
                    + follow_ups 
                    + "\n"
                    + res
                )
                early_exit = True
                break 
        
        if not early_exit:
            res = (
                self.format_reference(retrieval_result)
                + f"\nQuesiton: {question}"
                + "\nAre follow up questions needed here: "
                + follow_ups 
                + "\n"
                + res
                )
            
        item.update_output('retrieval_result', retrieval_result)
        item.update_output('pred', res)

    def run(self, dataset, do_eval=True, pred_process_fun=None):
        for item in tqdm(dataset, desc='Inference: '):
            self.run_item(item)
        
        dataset = self.evaluate(dataset, do_eval=do_eval, pred_process_fun=pred_process_fun)
        return dataset


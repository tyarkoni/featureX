'''
Extractors that operate primarily or exclusively on Text stimuli.
'''
import sys

from pliers.stimuli.text import TextStim, ComplexTextStim
from pliers.extractors.base import Extractor, ExtractorResult
from pliers.support.exceptions import PliersError
from pliers.support.decorators import requires_nltk_corpus
from pliers.datasets.text import fetch_dictionary
from pliers.transformers import BatchTransformerMixin
from pliers.utils import (attempt_to_import, verify_dependencies, flatten,
    listify)
import itertools
import numpy as np
import pandas as pd
import scipy
import nltk
from nltk.sentiment.vader import SentimentIntensityAnalyzer
import logging
from six import string_types

keyedvectors = attempt_to_import('gensim.models.keyedvectors', 'keyedvectors',
                                 ['KeyedVectors'])
sklearn_text = attempt_to_import('sklearn.feature_extraction.text', 'sklearn_text',
                                 ['CountVectorizer'])
spacy = attempt_to_import('spacy')
transformers = attempt_to_import('transformers')

class TextExtractor(Extractor):

    ''' Base Text Extractor class; all subclasses can only be applied to text.
    '''
    _input_type = TextStim


class ComplexTextExtractor(Extractor):

    ''' Base ComplexTextStim Extractor class; all subclasses can only be
    applied to ComplexTextStim instance.
    '''
    _input_type = ComplexTextStim

    def _extract(self, stim):
        ''' Returns all words. '''
        props = [(e.text, e.onset, e.duration) for e in stim.elements]
        vals, onsets, durations = map(list, zip(*props))
        return ExtractorResult(vals, stim, self, ['word'], onsets, durations)


class DictionaryExtractor(TextExtractor):

    ''' A generic dictionary-based extractor that supports extraction of
    arbitrary features contained in a lookup table.

    Args:
        dictionary (str, DataFrame): The dictionary containing the feature
            values. Either a string giving the path to the dictionary file,
            or a pandas DF. Format must be tab-delimited, with the first column
            containing the text key used for lookup. Subsequent columns each
            represent a single feature that can be used in extraction.
        variables (list): Optional subset of columns to keep from the
            dictionary.
        missing: Value to insert if no lookup value is found for a text token.
            Defaults to numpy's NaN.
    '''

    _log_attributes = ('dictionary', 'variables', 'missing')
    VERSION = '1.0'

    def __init__(self, dictionary, variables=None, missing=np.nan):
        if isinstance(dictionary, string_types):
            self.dictionary = dictionary  # for TranformationHistory logging
            dictionary = pd.read_csv(dictionary, sep='\t', index_col=0)
        else:
            self.dictionary = None
        self.data = dictionary
        if variables is None:
            variables = list(self.data.columns)
        else:
            self.data = self.data[variables]
        self.variables = variables
        # Set up response when key is missing
        self.missing = missing
        super(DictionaryExtractor, self).__init__()

    def _extract(self, stim):
        if stim.text not in self.data.index:
            vals = pd.Series(self.missing, self.variables)
        else:
            vals = self.data.loc[stim.text].fillna(self.missing)
        vals = vals.to_dict()
        return ExtractorResult(np.array([list(vals.values())]), stim, self,
                               features=list(vals.keys()))


class PredefinedDictionaryExtractor(DictionaryExtractor):

    ''' A generic Extractor that maps words onto values via one or more
    pre-defined dictionaries accessed via the web.

    Args:
        variables (list or dict): A specification of the dictionaries and
            column names to map the input TextStims onto. If a list, each
            element must be a string with the format 'dict/column', where the
            value before the slash gives the name of the dictionary, and the
            value after the slash gives the name of the column in that
            dictionary. These names can be found in the dictionaries.json
            specification file under the datasets submodule. Examples of
            valid values are 'affect/V.Mean.Sum' and
            'subtlexusfrequency/Lg10WF'. If a dict, the keys are the names of
            the dictionary files (e.g., 'affect'), and the values are lists
            of columns to use (e.g., ['V.Mean.Sum', 'V.SD.Sum']).
        missing (object): Value to use when an entry for a word is missing in
            a dictionary (defaults to numpy's NaN).
        case_sensitive (bool): If True, entries in the dictionary are treated
            as case-sensitive (e.g., 'John' and 'john' are different words).
        force_retrieve (bool): If True, the source dictionary will always be
            retrieved/download, even if it exists locally. If False, a cached
            local version will be used if it exists.
    '''

    _log_attributes = ('variables', 'missing', 'case_sensitive')
    VERSION = '1.0'

    def __init__(self, variables, missing=np.nan, case_sensitive=False,
                 force_retrieve=False):

        self.case_sensitive = case_sensitive

        if isinstance(variables, (list, tuple)):
            _vars = {}
            for v in variables:
                v = v.split('/')
                if v[0] not in _vars:
                    _vars[v[0]] = []
                if len(v) == 2:
                    _vars[v[0]].append(v[1])
            variables = _vars

        dicts = []
        for k, v in variables.items():
            d = fetch_dictionary(k, force_retrieve=force_retrieve)
            if not case_sensitive:
                d.index = d.index.str.lower()
            if v:
                d = d[v]
            d.columns = ['%s_%s' % (k, c) for c in d.columns]
            dicts.append(d)

        # Make sure none of the dictionaries have duplicate indices
        drop_dups = lambda d: d[~d.index.duplicated(keep='first')]
        dicts = [d if d.index.is_unique else drop_dups(d) for d in dicts]

        dictionary = pd.concat(dicts, axis=1, join='outer', sort=False)

        super(PredefinedDictionaryExtractor, self).__init__(
            dictionary, missing=missing)


class LengthExtractor(TextExtractor):

    ''' Extracts the length of the text in characters. '''

    VERSION = '1.0'

    def _extract(self, stim):
        return ExtractorResult(np.array([[len(stim.text.strip())]]), stim,
                               self, features=['text_length'])


class NumUniqueWordsExtractor(TextExtractor):

    ''' Extracts the number of unique words used in the text. '''

    _log_attributes = ('tokenizer',)
    VERSION = '1.0'

    def __init__(self, tokenizer=None):
        super(NumUniqueWordsExtractor, self).__init__()
        self.tokenizer = tokenizer

    @requires_nltk_corpus
    def _extract(self, stim):
        text = stim.text
        if self.tokenizer is None:
            if nltk is None:
                num_words = len(set(text.split()))
            else:
                num_words = len(set(nltk.word_tokenize(text)))
        else:
            num_words = len(set(self.tokenizer.tokenize(text)))

        return ExtractorResult(np.array([[num_words]]), stim, self,
                               features=['num_unique_words'])


class PartOfSpeechExtractor(BatchTransformerMixin, TextExtractor):

    ''' Tags parts of speech in text with nltk. '''

    _batch_size = sys.maxsize
    VERSION = '1.0'

    @requires_nltk_corpus
    def _extract(self, stims):
        words = [w.text for w in stims]
        pos = nltk.pos_tag(words)
        if len(words) != len(pos):
            raise PliersError(
                "The number of words does not match the number of tagged words"
                "returned by nltk's part-of-speech tagger.")

        results = []
        tagset = nltk.data.load('help/tagsets/upenn_tagset.pickle').keys()
        for i, s in enumerate(stims):
            pos_vector = dict.fromkeys(tagset, 0)
            pos_vector[pos[i][1]] = 1
            values = [list(pos_vector.values())]
            results.append(ExtractorResult(values, s, self,
                                           features=list(pos_vector.keys())))

        return results


class WordEmbeddingExtractor(TextExtractor):

    ''' An extractor that uses a word embedding file to look up embedding
    vectors for text.

    Args:
        embedding_file (str): Path to a word embedding file. Assumed to be in
            word2vec format compatible with gensim.
        binary (bool): Flag indicating whether embedding file is saved in a
            binary format.
        prefix (str): Prefix for feature names in the ExtractorResult.
        unk_vector (numpy array or str): Default vector to use for texts not
            found in the embedding file. If None is specified, uses a
            vector with all zeros. If 'random' is specified, uses a vector with
            random values between -1.0 and 1.0. Must have the same dimensions
            as the embeddings.
    '''

    _log_attributes = ('wvModel', 'prefix')

    def __init__(self, embedding_file, binary=False, prefix='embedding_dim',
                 unk_vector=None):
        verify_dependencies(['keyedvectors'])
        self.wvModel = keyedvectors.KeyedVectors.load_word2vec_format(
            embedding_file, binary=binary)
        self.prefix = prefix
        self.unk_vector = unk_vector
        super(WordEmbeddingExtractor, self).__init__()

    def _extract(self, stim):
        num_dims = self.wvModel.vector_size
        if stim.text in self.wvModel:
            embedding_vector = self.wvModel[stim.text]
        else:
            unk = self.unk_vector
            if hasattr(unk, 'shape') and unk.shape[0] == num_dims:
                embedding_vector = unk
            elif unk == 'random':
                embedding_vector = 2.0 * np.random.random(num_dims) - 1.0
            else:
                # By default, UNKs will have zeroed-out vectors
                embedding_vector = np.zeros(num_dims)

        features = ['%s%d' % (self.prefix, i) for i in range(num_dims)]
        return ExtractorResult([embedding_vector],
                               stim,
                               self,
                               features=features)


class TextVectorizerExtractor(BatchTransformerMixin, TextExtractor):

    ''' Uses a scikit-learn Vectorizer to extract bag-of-features
    from text.

    Args:
        vectorizer (sklearn Vectorizer or str): a scikit-learn Vectorizer
            (or the name in a string) to extract with. Will use the
            CountVectorizer by default. Uses supporting *args and **kwargs.
    '''

    _log_attributes = ('vectorizer',)
    _batch_size = sys.maxsize

    def __init__(self, vectorizer=None, *vectorizer_args, **vectorizer_kwargs):
        verify_dependencies(['sklearn_text'])
        if isinstance(vectorizer, sklearn_text.CountVectorizer):
            self.vectorizer = vectorizer
        elif isinstance(vectorizer, str):
            vec = getattr(sklearn_text, vectorizer)
            self.vectorizer = vec(*vectorizer_args, **vectorizer_kwargs)
        else:
            self.vectorizer = sklearn_text.CountVectorizer(*vectorizer_args,
                                                           **vectorizer_kwargs)
        super(TextVectorizerExtractor, self).__init__()

    def _extract(self, stims):
        mat = self.vectorizer.fit_transform([s.text for s in stims]).toarray()
        results = []
        for i, row in enumerate(mat):
            results.append(
                ExtractorResult([row], stims[i], self,
                                features=self.vectorizer.get_feature_names()))
        return results


class VADERSentimentExtractor(TextExtractor):

    ''' Uses nltk's VADER lexicon to extract (0.0-1.0) values for the positve,
    neutral, and negative sentiment of a TextStim. Also returns a compound
    score ranging from -1 (very negative) to +1 (very positive). '''

    _log_attributes = ('analyzer',)
    VERSION = '1.0'

    def __init__(self):
        self.analyzer = SentimentIntensityAnalyzer()
        super(VADERSentimentExtractor, self).__init__()

    @requires_nltk_corpus
    def _extract(self, stim):
        scores = self.analyzer.polarity_scores(stim.text)
        features = ['sentiment_' + k for k in scores.keys()]
        return ExtractorResult([list(scores.values())], stim, self,
                               features=features)


class SpaCyExtractor(TextExtractor):

    ''' A generic class for Spacy Text extractors

    Uses SpaCy to extract features from text. Extracts features for every word
    (token) in a sentence.

    Args:
        extractor_type(str): The type of feature to extract. Must be one of
            'doc' (analyze an entire sentence/document) or 'token'
            (analyze each word).
        features(list): A list of strings giving the names of spaCy features to
            extract. See SpacY documentation for details. By default, returns
            all available features for the given extractor type.
        model (str): The name of the language model to use.
    '''

    def __init__(self, extractor_type='token', features=None,
                 model='en_core_web_sm'):

        verify_dependencies(['spacy'])

        try:
            self.model = spacy.load(model)
        except (ImportError, IOError, OSError) as e:
            logging.warning("Spacy Models ('{}') not found. Downloading and"
                            "installing".format(model))

            spacy.cli.download(model)
            self.model = spacy.load(model)

        logging.info('Loaded model: {}'.format(self.model))

        self.features = features
        self.extractor_type = extractor_type.lower()

        super(SpaCyExtractor, self).__init__()

    def _extract(self, stim):

        features_list = []
        elements = self.model(stim.text)
        order_list = []

        if self.extractor_type == 'token':
            if self.features is None:
                self.features = ['text', 'lemma_', 'pos_', 'tag_', 'dep_',
                                 'shape_', 'is_alpha', 'is_stop', 'is_punct',
                                 'sentiment', 'is_ascii', 'is_digit']

        elif self.extractor_type == 'doc':
            elements = [elem.as_doc() for elem in list(elements.sents)]
            if self.features is None:
                self.features = ['text', 'is_tagged', 'is_parsed',
                                 'is_sentenced', 'sentiment']

        else:
            raise(ValueError("Invalid extractor_type; must be one of 'token'"
                             " or 'doc'."))

        features_list = []
        for elem in elements:
            arr = []
            for feat in self.features:
                arr.append(getattr(elem, feat))
            features_list.append(arr)

        order_list = list(range(1, len(elements) + 1))

        return ExtractorResult(features_list, stim, self,
                               features=self.features, orders=order_list)


class BertExtractor(ComplexTextExtractor):

    ''' Base class for all Extractors based on pretrained BERT.
        This model returns the last hidden layer (wihtout special tokens)

    Args:
        pretrained_model (str): A string specifying which BERT
            model to use. Can be one of pretrained BERT models listed at
            https://huggingface.co/transformers/pretrained_models.html
            (valid values include all the models with 'bert' prefix) 
            or path to custom model.
        tokenizer (str): Type of tokenization used in the tokenization step.
            If different from model, out-of-vocabulary tokens may be treated 
            as unknown tokens.
        model_class (str): Specifies class of Bert model. Must be one of 
            'BertModel' or 'BertForLM'.
        framework (str): name deep learning framework to use. Must be 'pt'
            (PyTorch) or 'tf' (tensorflow). Defaults to 'pt'.
        return_metadata (bool): if True, the extractor returns encoded token
            and encoded word as features.
        model_kwargs (dict): Named arguments for transformer model.
            See https://huggingface.co/transformers/main_classes/model.html
        tokenizer_kwargs (dict): Named arguments for tokenizer.
            See https://huggingface.co/transformers/main_classes/tokenizer.html
    '''

    _log_attributes = ('pretrained_model', 'framework', 'tokenizer_type',
        'model_class', 'model_kwargs', 'tokenizer_kwargs')

    def __init__(self,
                 pretrained_model='bert-base-uncased',
                 tokenizer='bert-base-uncased',
                 model_class='BertModel',
                 framework='pt',
                 return_metadata=False,
                 model_kwargs=None,
                 tokenizer_kwargs=None):
                 
        verify_dependencies(['transformers'])

        if framework not in ['pt', 'tf']:
            raise(ValueError('''Invalid framework;
                must be one of 'pt' (pytorch) or 'tf' (tensorflow)'''))
        self.pretrained_model = pretrained_model
        self.tokenizer_type = tokenizer
        self.model_class = model_class
        self.framework = framework
        self.return_metadata = return_metadata
        self.model_kwargs = model_kwargs if model_kwargs else {}
        self.tokenizer_kwargs = tokenizer_kwargs if tokenizer_kwargs else {}

        model = model_class if self.framework == 'pt' else 'TF' + model_class
        self.model = getattr(transformers, model).from_pretrained(
            pretrained_model, **self.model_kwargs)
        self.tokenizer = transformers.BertTokenizer.from_pretrained(
            tokenizer, **self.tokenizer_kwargs)
        super(BertExtractor, self).__init__()

    def _mask(self, wds, mask):
        return wds

    def _preprocess(self, stims, mask):
        els = [(e.text, e.onset, e.duration) for e in stims.elements]
        wds, ons, dur = map(list, zip(*els))
        tok = [self.tokenizer.tokenize(w) for w in self._mask(wds, mask)]
        n_tok = [len(t) for t in tok]
        wds, ons, dur = map(lambda x: np.repeat(x, n_tok), [wds, ons, dur])
        tok = list(flatten(tok))
        idx = self.tokenizer.encode(tok, return_tensors=self.framework)
        return wds, ons, dur, tok, idx

    def _extract(self, stims):
        mask = self.mask or None
        wds, ons, dur, tok, idx = self._preprocess(stims, mask=mask)
        preds = self.model(idx)
        preds = [p.detach() if self.framework == 'pt' else p for p in preds]
        data, feat, ons, dur = self._postprocess(preds, tok, wds, ons, dur)
        return ExtractorResult(data, stims, self, features=feat, onsets=ons, 
                               durations=dur)

    def _postprocess(self, preds, tok, wds, ons, dur):
        out = preds[0][:, 1:-1, :].numpy().squeeze()
        data = [out.tolist()]
        feat = ['encoding']
        if self.return_metadata:
            data += [tok, wds]
            feat += ['token', 'word']
        return data, feat, ons, dur

    def _get_model_attributes(self):
        return ['pretrained_model', 'framework', 'model_class', 
                'tokenizer_type']

    def _to_df(self, result, include_attributes=True):
        res_dict = dict(zip(result.features, result._data))
        if include_attributes:
            log_dict = {attr: getattr(result.extractor, attr) for
                        attr in self._get_model_attributes()}
            res_dict.update(log_dict)
        res_df = pd.DataFrame(res_dict)
        res_df['object_id'] = range(res_df.shape[0])
        return res_df

class BertSequenceEncodingExtractor(BertExtractor):

    ''' Extract contextualized encodings for words or sequences using
        pretrained BertModel.
    Args:
        pretrained_model (str): A string specifying which BERT
            model to use. Can be one of pretrained BERT models listed at
            https://huggingface.co/transformers/pretrained_models.html
            (valid values include all the models with 'bert' prefix) 
            or path to custom model.
        tokenizer (str): Type of tokenization used in the tokenization step.
            If different from model, out-of-vocabulary tokens may be treated as
            unknown tokens.
        framework (str): name deep learning framework to use. Must be 'pt'
            (PyTorch) or 'tf' (tensorflow). Defaults to 'pt'.
        pooling (str): defines whether to return encoding for [CLS] token 
            (None, default), or the numpy function to use to pool token-level 
            encodings.
        return_sep (bool): defines whether to return encoding for the [SEP]
            token.
        return_metadata (bool): If True, the extractor returns an additional 
            feature column with the encoded sequence.
        model_kwargs (dict): Named arguments for pretrained model.
            See: https://huggingface.co/transformers/main_classes/model.html
            and https://huggingface.co/transformers/model_doc/bert.html
        tokenizer_kwargs (dict): Named arguments for tokenizer.
            See https://huggingface.co/transformers/main_classes/tokenizer.html
    '''

    _log_attributes = ('pretrained_model', 'framework', 'tokenizer_type', 
                       'pooling', 'return_sep', 'model_class', 'model_kwargs',
                       'tokenizer_kwargs')

    def __init__(self,
                 pretrained_model='bert-base-uncased',
                 tokenizer='bert-base-uncased',
                 framework='pt',
                 pooling=None,
                 return_sep=False,
                 return_metadata=False,
                 model_kwargs=None,
                 tokenizer_kwargs=None):

        super(BertSequenceEncodingExtractor, self).__init__(pretrained_model,
            tokenizer, framework, return_metadata, model_kwargs, 
            tokenizer_kwargs, model_class='BertModel')
        if pooling:
            if return_sep:
                raise(ValueError('Pooling and return_seq argument are '
                'mutually exclusive.'))
            try: 
                getattr(np, pooling)
            except:
                raise(ValueError('Pooling must be a valid numpy function.'))
        self.pooling = pooling
        self.return_sep = return_sep
    
    def _postprocess(self, preds, tok, wds, ons, dur):
        preds = [p.numpy().squeeze() for p in preds]
        tok = [' '.join(wds)]
        try: 
            dur = ons[-1] + dur[-1] - ons[0]
        except:
            dur = None
        ons = ons[0]
        if self.return_sep:
            out = preds[0][:,-1,:]
        elif self.pooling:
            pool_func = getattr(np, self.pooling)
            out = pool_func(preds[0][:, 1:-1, :], axis=1, keepdims=True)
        else:
            out = preds[1]
        data = [out.tolist()]
        feat = ['encoding']
        if self.return_metadata:
            data += [tok]
            feat += ['sequence']   
        return data, feat, ons, dur
    
    def _get_model_attributes(self):
        return ['pretrained_model', 'framework', 'model_class', 
                'pooling', 'return_sep', 'tokenizer_type']


class BertLMExtractor(BertExtractor):

    ''' Use BERT for masked words prediction.

    Args:
        pretrained_model (str): A string specifying which BERT
            model to use. Can be one of pretrained BERT models listed at
            https://huggingface.co/transformers/pretrained_models.html
            (valid values include all the models with 'bert' prefix) 
            or path to custom model.
        tokenizer (str): Type of tokenization used in the tokenization step.
            If different from model, out-of-vocabulary tokens may be treated as
            unknown tokens.
        framework (str): name deep learning framework to use. Must be 'pt'
            (PyTorch) or 'tf' (tensorflow). Defaults to 'pt'.
        mask (int or str): Words to be masked (string) or indices of 
            words in the sequence to be masked (indexing starts at 0). Can
            be either a single word/index or a list of words/indices.
            If str is passed and more than one word in the input matches 
            the string, only the first one is masked. 
        top_n (int): Specifies how many of the highest-probability tokens are
            to be returned. Mutually exclusive with target and threshold.
        target (str or list): Vocabulary token(s) for which probability is to 
            be returned. Tokens defined in the vocabulary change across 
            tokenizers. Mutually exclusive with top_n and threshold.
        threshold (float): If defined, only values above this threshold will
            be returned. Mutually exclusive with top_n and target.
        return_softmax (bool): if True, returns probability scores instead of 
            raw predictions scores.
        return_metadata (bool): if True, returns masked word (if defined in the
            tokenizer vocabulary) and its probability.
        model_kwargs (dict): Named arguments for pretrained model.
            See: https://huggingface.co/transformers/main_classes/model.html
            and https://huggingface.co/transformers/model_doc/bert.html.
        tokenizer_kwargs (dict): Named arguments for tokenizer.
            See https://huggingface.co/transformers/main_classes/tokenizer.html.
    '''

    _log_attributes = ('pretrained_model', 'framework', 'top_n', 'target', 'tokenizer_type', 'return_softmax')

    def __init__(self,
                 pretrained_model='bert-base-uncased',
                 tokenizer='bert-base-uncased',
                 framework='pt',
                 mask='[MASK]',
                 top_n=100,
                 threshold=None,
                 target=None,
                 return_softmax=False,
                 return_true=False,
                 model_kwargs=None,
                 tokenizer_kwargs=None):
        
        super(BertLMExtractor, self).__init__(pretrained_model=pretrained_model, 
                                              tokenizer=tokenizer, 
                                              framework=framework,
                                              model_kwargs=model_kwargs,
                                              tokenizer_kwargs=tokenizer_kwargs,
                                              model_class='BertForMaskedLM')
        if any([top_n and target, top_n and threshold, threshold and target]):
            raise ValueError('top_n, threshold and target arguments '
                             'are mutually exclusive')
        self.top_n = top_n
        self.threshold = threshold
        self.target = listify(target)
        if self.target:
            missing = set(self.target) - set(self.tokenizer.vocab.keys())
            if missing:
                logging.warning(f'{missing} is not in vocabulary. Dropping.')
            self.target = set(self.target) & set(self.tokenizer.vocab.keys())
            if self.target == []:
                raise ValueError('No valid target token. Import transformers'
                    ' and run transformers.BertTokenizer.from_pretrained'
                    f'(\'{tokenizer}\').vocab.keys() to see available tokens')
        self.return_softmax = return_softmax
        self.return_true = return_true
        self.mask = mask

    def _mask(self, wds, mask):
        if not type(mask) in [int, str]:
            raise ValueError('mask argument must be an integer or a string')
        mwds = wds.copy()
        self.mask_token = mask if type(mask) == str else mwds[mask]
        self.mask_pos = np.where(np.array(mwds)==self.mask_token)[0][0]
        mwds[self.mask_pos] = '[MASK]'
        return mwds

    def _postprocess(self, preds, tok, wds, ons, dur):
        preds = preds[0].numpy()[:,1:-1,:]
        if self.return_softmax:
            preds = scipy.special.softmax(preds, axis=-1)
        out_idx = preds[0,self.mask_pos,:].argsort(axis=-1)[::-1]
        if self.top_n:
            sub_idx = range(self.top_n)
        elif self.target:
            sub_idx = self.tokenizer.convert_tokens_to_ids(self.target)
        elif self.threshold:
            sub_idx = np.where(preds[0,self.mask_pos,:] > self.threshold)[0]
        out_idx = list(set(out_idx) & set(sub_idx)) if sub_idx else out_idx
        feat = self.tokenizer.convert_ids_to_tokens(out_idx)
        data = preds[0,self.mask_pos,out_idx]
        if self.return_metadata:
            feat, data = self._return_true_token(preds, feat, data)
        ons, dur = map(lambda x: listify(x[self.mask_pos]), [ons, dur])
        return data, feat, ons, dur

    def _return_true_token(self, preds, feat, data):
        if self.mask_token in self.tokenizer.vocab:
            true_vocab_idx = self.tokenizer.vocab[self.mask_token]
            true_score = preds[0, self.mask_pos, true_vocab_idx]
        else:
            true_score = np.nan
            logging.warning('True token not in vocabulary. Returning NaN')
        feat += ['true_word', 'true_word_score']
        data += [self.mask_token, true_score]
        return feat, data
    
    def _get_model_attributes(self):
        return ['pretrained_model', 'framework', 'top_n', 'mask',
         'target', 'threshold', 'tokenizer_type']

# To discuss:
# What to do with SEP token? Does it need to be there?
# Return other layers and/or attentions?
# Couple of mixins (sequence coherence, probability)
# Look into the sentiment extractor
# Discuss probability mixin with Tal

# To dos:
# Metadata as features / Add other field to store additional info (?)
# Log input sequence in LM extractor
# NB: a bit suboptimal to set mask in init, but handier

class WordCounterExtractor(ComplexTextExtractor):

    ''' Extracts number of times each unique word has occurred within text

    Args:
        log_scale(bool): specifies if count values are to be returned in log-
                         scale (defaults to False)
        '''

    _log_attributes = ('case_sensitive', 'log_scale')

    def __init__(self, case_sensitive=False, log_scale=False):
        self.log_scale = log_scale
        self.case_sensitive = case_sensitive
        self.features = ['log_word_count'] if self.log_scale else ['word_count']
        super(WordCounterExtractor, self).__init__()

    def _extract(self, stims):
        onsets = [s.onset for s in stims]
        durations = [s.duration for s in stims]
        tokens = [s.text for s in stims]
        tokens = [t if self.case_sensitive else t.lower() for t in tokens]
        word_counter = pd.Series(tokens).groupby(tokens).cumcount() + 1
        if self.log_scale:
            word_counter = np.log(word_counter)

        return ExtractorResult(word_counter, stims, self,
                               features=self.features,
                               onsets=onsets, durations=durations)

import os
import csv
import uuid
import glob
import shutil
import tempfile

from pandas import DataFrame

from . import wrapper
from wrapper import constants
from . import master_component as mc

from . import batches_utils as bu
from . import regularizers
from . import scores
from . import score_tracker as st

SCORE_TRACKER = {
    constants.ScoreConfig_Type_SparsityPhi: st.SparsityPhiScoreTracker,
    constants.ScoreConfig_Type_SparsityTheta: st.SparsityThetaScoreTracker,
    constants.ScoreConfig_Type_Perplexity: st.PerplexityScoreTracker,
    constants.ScoreConfig_Type_ThetaSnippet: st.ThetaSnippetScoreTracker,
    constants.ScoreConfig_Type_ItemsProcessed: st.ItemsProcessedScoreTracker,
    constants.ScoreConfig_Type_TopTokens: st.TopTokensScoreTracker,
    constants.ScoreConfig_Type_TopicKernel: st.TopicKernelScoreTracker
}


class ARTM(object):
    """ARTM represents a topic model (public class)

    Args:
      num_processors (int): how many threads will be used for model training,
      if not specified then number of threads will be detected by the lib
      topic_names (list of str): names of topics in model, if not specified will be
      auto-generated by lib according to num_topics
      num_topics (int): number of topics in model (is used if topic_names
      not specified), default=10
      class_ids (dict): list of class_ids and their weights to be used in model,
      key --- class_id, value --- weight, if not specified then all class_ids
      will be used
      num_document_passes (int): number of iterations over each document
      during processing, default=10
      cache_theta (bool): save or not the Theta matrix in model. Necessary
      if ARTM.get_theta() usage expects, default=True

    Important public fields:
      regularizers: contains dict of regularizers, included into model
      scores: contains dict of scores, included into model
      score_tracker: contains dict of scoring results;
      key --- score name, value --- ScoreTracker object, which contains info about
      values of score on each synchronization in list

    NOTE:
      - Here and anywhere in BigARTM empty topic_names or class_ids means that
      model (or regularizer, or score) should use all topics or class_ids.
      - If some fields of regularizers or scores are not defined by
      user --- internal lib defaults would be used.
      - If field 'topics_name' == [], it will be generated by BigARTM and will
      be available using ARTM.topics_name().
    """

    # ========== CONSTRUCTOR ==========
    def __init__(self, num_processors=0, topic_names=None, num_topics=10,
                 class_ids=None, num_document_passes=10, cache_theta=True):
        self._num_processors = 0
        self._num_topics = 10
        self._num_document_passes = 10
        self._cache_theta = True

        if topic_names is None or not topic_names:
            self._topic_names = []
            if num_topics > 0:
                self._num_topics = num_topics
        else:
            self._topic_names = topic_names
            self._num_topics = len(topic_names)

        if class_ids is None:
            self._class_ids = {}
        elif len(class_ids) > 0:
            self._class_ids = class_ids

        if num_processors > 0:
            self._num_processors = num_processors

        if num_document_passes > 0:
            self._num_document_passes = num_document_passes

        if isinstance(cache_theta, bool):
            self._cache_theta = cache_theta

        self._lib = wrapper.LibArtm()
        self._master = mc.MasterComponent(self._lib,
                                          num_processors=self._num_processors,
                                          cache_theta=self._cache_theta)

        self._model = 'pwt'
        self._regularizers = regularizers.Regularizers(self._master)
        self._scores = scores.Scores(self._master, self._model)

        self._score_tracker = {}
        self._synchronizations_processed = 0
        self._initialized = False

    # ========== PROPERTIES ==========
    @property
    def num_processors(self):
        return self._num_processors

    @property
    def num_document_passes(self):
        return self._num_document_passes

    @property
    def cache_theta(self):
        return self._cache_theta

    @property
    def num_topics(self):
        return self._num_topics

    @property
    def topic_names(self):
        return self._topic_names

    @property
    def class_ids(self):
        return self._class_ids

    @property
    def regularizers(self):
        return self._regularizers

    @property
    def scores(self):
        return self._scores

    @property
    def score_tracker(self):
        return self._score_tracker

    @property
    def master(self):
        return self._master

    @property
    def model(self):
        return self._model

    @property
    def num_phi_updates(self):
        return self._synchronizations_processed

    # ========== SETTERS ==========
    @num_processors.setter
    def num_processors(self, num_processors):
        if num_processors <= 0 or not isinstance(num_processors, int):
            raise IOError('Number of processors should be a positive integer')
        else:
            self.master.reconfigure(num_processors=num_processors)
            self._num_processors = num_processors

    @num_document_passes.setter
    def num_document_passes(self, num_document_passes):
        if num_document_passes <= 0 or not isinstance(num_document_passes, int):
            raise IOError("Number of passes through documents" +
                          "should be a positive integer")
        else:
            self._num_document_passes = num_document_passes

    @cache_theta.setter
    def cache_theta(self, cache_theta):
        if not isinstance(cache_theta, bool):
            raise IOError('cache_theta should be bool')
        else:
            self.master.reconfigure(cache_theta=cache_theta)
            self._cache_theta = cache_theta

    @num_topics.setter
    def num_topics(self, num_topics):
        if num_topics <= 0 or not isinstance(num_topics, int):
            raise IOError('Number of topics should be a positive integer')
        else:
            self._num_topics = num_topics

    @topic_names.setter
    def topic_names(self, topic_names):
        if not topic_names:
            raise IOError('Number of topic names should be non-negative')
        else:
            self._topic_names = topic_names
            self._num_topics = len(topic_names)

    @class_ids.setter
    def class_ids(self, class_ids):
        if len(class_ids) < 0:
            raise IOError('Number of (class_id, class_weight) pairs should be non-negative')
        else:
            self._class_ids = class_ids

    # ========== PRIVATE ==========
    def _parse_collection_inline(self, target_folder, data_path, data_format,
                                 collection_name=None, batches=None, batch_size=None):
        batches_list = []
        if data_format == 'batches':
            if batches is None:
                batches_list = glob.glob(os.path.join(data_path, '*.batch'))
                if len(batches_list) < 1:
                    raise RuntimeError('No batches were found')
            else:
                batches_list = [os.path.join(data_path, batch) for batch in batches]

        elif data_format == 'bow_uci' or data_format == 'vowpal_wabbit':
            collection_parser_config = bu._create_parser_config(
                data_path=data_path,
                collection_name=collection_name,
                target_folder=target_folder,
                batch_size=batch_size,
                data_format=data_format)

            self._lib.ArtmParseCollection(self._master.master_id, collection_parser_config)
            batches_list = glob.glob(os.path.join(target_folder, '*.batch'))

        elif data_format == 'plain_text':
            raise NotImplementedError()
        else:
            raise IOError('Unknown data format')

        return batches_list

    # ========== METHODS ==========
    def load_dictionary(self, dictionary_name=None, dictionary_path=None):
        """ARTM.load_dictionary() --- load the BigARTM dictionary of
        the collection into the lib

        Args:
          dictionary_name (str): the name of the dictionary in the lib, default=None
          dictionary_path (str): full file name of the dictionary, default=None
        """
        if dictionary_path is not None and dictionary_name is not None:
            self.master.import_dictionary(file_name=dictionary_path,
                                          dictionary_name=dictionary_name)
        elif dictionary_path is None:
            raise IOError('dictionary_path is None')
        else:
            raise IOError('dictionary_name is None')

    def remove_dictionary(self, dictionary_name=None):
        """ARTM.remove_dictionary() --- remove the loaded BigARTM dictionary
        from the lib

        Args:
          dictionary_name (str): the name of the dictionary in th lib, default=None
        """
        if dictionary_name is not None:
            self._lib.ArtmDisposeDictionary(self.master.master_id, dictionary_name)
        else:
            raise IOError('dictionary_name is None')

    def fit_offline(self, collection_name=None, batches=None, data_path='',
                    num_collection_passes=1, decay_weight=0.0, apply_weight=1.0,
                    reset_theta_scores=False, data_format='batches', batch_size=1000):
        """ARTM.fit_offline() --- proceed the learning of
        topic model in off-line mode

        Args:
          collection_name (str): the name of text collection (required if
          data_format == 'bow_uci'), default=None
          batches (list of str): list of file names of batches to be processed.
          If not None, than data_format should be 'batches'. Format --- '*.batch',
          default=None
          data_path (str):
          1) if data_format == 'batches' => folder containing batches and dictionary;
          2) if data_format == 'bow_uci' => folder containing
            docword.collection_name.txt and vocab.collection_name.txt files;
          3) if data_format == 'vowpal_wabbit' => file in Vowpal Wabbit format;
          4) if data_format == 'plain_text' => file with text;
          default=''
          num_collection_passes (int): number of iterations over whole given
          collection, default=1
          decay_weight (int): coefficient for applying old n_wt counters,
          default=0.0 (apply_weight + decay_weight = 1.0)
          apply_weight (int): coefficient for applying new n_wt counters,
          default=1.0 (apply_weight + decay_weight = 1.0)
          reset_theta_scores (bool): reset accumulated Theta scores
          before learning, default=False
          data_format (str): the type of input data;
          1) 'batches' --- the data in format of BigARTM;
          2) 'bow_uci' --- Bag-Of-Words in UCI format;
          3) 'vowpal_wabbit' --- Vowpal Wabbit format;
          4) 'plain_text' --- source text;
          default='batches'

          Next argument has sense only if data_format is not 'batches'
          (e.g. parsing is necessary).
            batch_size (int): number of documents to be stored ineach batch,
            default=1000

        Note:
          ARTM.initialize() should be proceed before first call
          ARTM.fit_offline(), or it will be initialized by dictionary
          during first call.
        """
        if collection_name is None and data_format == 'bow_uci':
            raise IOError('No collection name was given')

        target_folder = data_path
        if not data_format == 'batches':
            target_folder = tempfile.mkdtemp()
        try:
            batches_list = self._parse_collection_inline(target_folder=target_folder,
                                                         data_path=data_path,
                                                         data_format=data_format,
                                                         collection_name=collection_name,
                                                         batches=batches,
                                                         batch_size=batch_size)

            if not self._initialized:
                dictionary_name = bu.DICTIONARY_NAME + str(uuid.uuid4())
                self.master.import_dictionary(
                    self,
                    dictionary_name=dictionary_name,
                    file_name=os.path.join(target_folder, bu.DICTIONARY_NAME))

                self.initialize(dictionary_name=dictionary_name)
                self.remove_dictionary(dictionary_name)

            theta_reg_name, theta_reg_tau, phi_reg_name, phi_reg_tau = [], [], [], []
            for name, config in self._regularizers.data.iteritems():
                if str(config.__class__.__bases__[0].__name__) == 'BaseRegularizerTheta':
                    theta_reg_name.append(name)
                    theta_reg_tau.append(config.tau)
                else:
                    phi_reg_name.append(name)
                    phi_reg_tau.append(config.tau)

            for _ in xrange(num_collection_passes):
                self.master.process_batches(pwt=self.model,
                                            batches=batches_list,
                                            nwt='nwt_hat',
                                            regularizer_name=theta_reg_name,
                                            regularizer_tau=theta_reg_tau,
                                            num_inner_iterations=self._num_document_passes,
                                            class_ids=self._class_ids,
                                            reset_scores=reset_theta_scores)
                self._synchronizations_processed += 1
                if self._synchronizations_processed == 1:
                    self.master.merge_model({self.model: decay_weight, 'nwt_hat': apply_weight},
                                            nwt='nwt', topic_names=self._topic_names)
                else:
                    self.master.merge_model({'nwt': decay_weight, 'nwt_hat': apply_weight},
                                            nwt='nwt', topic_names=self._topic_names)

                self.master.regularize_model(pwt=self.model,
                                             nwt='nwt',
                                             rwt='rwt',
                                             regularizer_name=phi_reg_name,
                                             regularizer_tau=phi_reg_tau)
                self.master.normalize_model(nwt='nwt', pwt=self.model, rwt='rwt')

                for name in self.scores.data.keys():
                    if name not in self.score_tracker:
                        self.score_tracker[name] =\
                            SCORE_TRACKER[self.scores[name].type](self.scores[name])

                        for _ in xrange(self._synchronizations_processed - 1):
                            self.score_tracker[name].add()

                    self.score_tracker[name].add(self.scores[name])

        finally:
            # Remove temp batches folder if it necessary
            if not data_format == 'batches':
                shutil.rmtree(target_folder)

    def fit_online(self, collection_name=None, batches=None, data_path='',
                   tau0=1024.0, kappa=0.7, update_every=1, reset_theta_scores=False,
                   data_format='batches', batch_size=1000):
        """ARTM.fit_online() --- proceed the learning of topic model
        in on-line mode

        Args:
          collection_name (str): the name of text collection (required if
          data_format == 'bow_uci'), default=None
          batches (list of str): list of file names of batches to be processed.
          If not None, than data_format should be 'batches'. Format --- '*.batch',
          default=None
          data_path (str):
          1) if data_format == 'batches' => folder containing batches and dictionary;
          2) if data_format == 'bow_uci' => folder containing
            docword.collection_name.txt and vocab.collection_name.txt files;
          3) if data_format == 'vowpal_wabbit' => file in Vowpal Wabbit format;
          4) if data_format == 'plain_text' => file with text;
          default=''
          update_every (int): the number of batches; model will be updated once per it,
          default=1
          tau0 (float): coefficient (see kappa), default=1024.0
          kappa (float): power for tau0, default=0.7

          The formulas for decay_weight and apply_weight:
          update_count = current_processed_docs / (batch_size * update_every)
          rho = pow(tau0 + update_count, -kappa)
          decay_weight = 1-rho
          apply_weight = rho

          reset_theta_scores (bool): reset accumulated Theta scores
          before learning, default=False
          data_format (str): the type of input data;
          1) 'batches' --- the data in format of BigARTM;
          2) 'bow_uci' --- Bag-Of-Words in UCI format;
          3) 'vowpal_wabbit' --- Vowpal Wabbit format;
          4) 'plain_text' --- source text;
          default='batches'

          Next argument has sense only if data_format is not 'batches'
          (e.g. parsing is necessary).
            batch_size (int): number of documents to be stored ineach batch,
            default=1000

        Note:
          ARTM.initialize() should be proceed before first call
          ARTM.fit_online(), or it will be initialized by dictionary
          during first call.
        """
        target_folder = data_path
        if not data_format == 'batches':
            target_folder = tempfile.mkdtemp()
        try:
            batches_list = self._parse_collection_inline(target_folder=target_folder,
                                                         data_path=data_path,
                                                         data_format=data_format,
                                                         collection_name=collection_name,
                                                         batches=batches,
                                                         batch_size=batch_size)

            if not self._initialized:
                dictionary_name = bu.DICTIONARY_NAME + str(uuid.uuid4())
                self.master.import_dictionary(
                    self,
                    dictionary_name=dictionary_name,
                    file_name=os.path.join(target_folder, bu.DICTIONARY_NAME))

                self.initialize(dictionary_name=dictionary_name)
                self.remove_dictionary(dictionary_name)

            theta_reg_name, theta_reg_tau, phi_reg_name, phi_reg_tau = [], [], [], []
            for name, config in self._regularizers.data.iteritems():
                if str(config.__class__.__bases__[0].__name__) == 'BaseRegularizerTheta':
                    theta_reg_name.append(name)
                    theta_reg_tau.append(config.tau)
                else:
                    phi_reg_name.append(name)
                    phi_reg_tau.append(config.tau)

            batches_to_process = []
            current_processed_documents = 0
            for batch_idx, batch_filename in enumerate(batches_list):
                batches_to_process.append(batch_filename)
                if ((batch_idx + 1) % update_every == 0) or ((batch_idx + 1) == len(batches_list)):
                    self.master.process_batches(pwt=self.model,
                                                batches=batches_to_process,
                                                nwt='nwt_hat',
                                                regularizer_name=theta_reg_name,
                                                regularizer_tau=theta_reg_tau,
                                                num_inner_iterations=self._num_document_passes,
                                                class_ids=self._class_ids,
                                                reset_scores=reset_theta_scores)

                    current_processed_documents += batch_size * update_every
                    update_count = current_processed_documents / (batch_size * update_every)
                    rho = pow(tau0 + update_count, -kappa)
                    decay_weight, apply_weight = 1 - rho, rho

                    self._synchronizations_processed += 1
                    if self._synchronizations_processed == 1:
                        self.master.merge_model(
                            models={self.model: decay_weight, 'nwt_hat': apply_weight},
                            nwt='nwt',
                            topic_names=self._topic_names)
                    else:
                        self.master.merge_model(
                            models={'nwt': decay_weight, 'nwt_hat': apply_weight},
                            nwt='nwt',
                            topic_names=self._topic_names)

                    self.master.regularize_model(pwt=self.model,
                                                 nwt='nwt',
                                                 rwt='rwt',
                                                 regularizer_name=phi_reg_name,
                                                 regularizer_tau=phi_reg_tau)

                    self.master.normalize_model(nwt='nwt', pwt=self.model, rwt='rwt')
                    batches_to_process = []

                    for name in self.scores.data.keys():
                        if name not in self.score_tracker:
                            self.score_tracker[name] =\
                                SCORE_TRACKER[self.scores[name].type](self.scores[name])

                            for _ in xrange(self._synchronizations_processed - 1):
                                self.score_tracker[name].add()

                        self.score_tracker[name].add(self.scores[name])
        finally:
            # Remove temp batches folder if it necessary
            if not data_format == 'batches':
                shutil.rmtree(target_folder)

    def save(self, file_name='artm_model'):
        """ARTM.save() --- save the topic model to disk

        Args:
          file_name (str): the name of file to store model, default='artm_model'
        """
        if not self._initialized:
            raise RuntimeError("Model does not exist yet. Use " +
                               "ARTM.initialize()/ARTM.fit_*()")

        if os.path.isfile(file_name):
            os.remove(file_name)
        self.master.export_model(self.model, file_name)

    def load(self, file_name):
        """ARTM.load() --- load the topic model,
        saved by ARTM.save(), from disk

        Args:
          file_name (str) --- the name of file containing model, no default

        Note:
          Loaded model will overwrite ARTM.topic_names and
          ARTM.num_topics fields. Also it will empty
          ARTM.score_tracker.
        """
        self.master.import_model(self.model, file_name)
        self._initialized = True
        topic_model = self.master.get_phi_info(model=self.model)
        self._topic_names = [topic_name for topic_name in topic_model.topic_name]
        self._num_topics = topic_model.topics_count

        # Remove all info about previous iterations
        self._score_tracker = {}
        self._synchronizations_processed = 0

    def get_phi(self, topic_names=None, class_ids=None):
        """ARTM.get_phi() --- get Phi matrix of model

        Args:
          topic_names (list of str): list with topics to extract,
          default=None (means all topics)
          class_ids (list of str): list with class ids to extract,
          default=None (means all class ids)

        Returns:
          pandas.DataFrame: (data, columns, rows), where:
          1) columns --- the names of topics in topic model
          2) rows --- the tokens of topic model
          3) data --- content of Phi matrix
        """
        if not self._initialized:
            raise RuntimeError("Model does not exist yet. Use " +
                               "ARTM.initialize()/ARTM.fit_*()")

        phi_info = self.master.get_phi_info(model=self.model)
        nd_array = self.master.get_phi_matrix(model=self.model,
                                              topic_names=topic_names,
                                              class_ids=class_ids)

        tokens = [token for token in phi_info.token]
        topic_names = [topic_name for topic_name in phi_info.topic_name]
        phi_data_frame = DataFrame(data=nd_array,
                                   columns=topic_names,
                                   index=tokens)

        return phi_data_frame

    def fit_transform(self, topic_names=None, remove_theta=False):
        """ARTM.fit_transform() --- get Theta matrix for training set
        of documents

        Args:
          topic_names (list of str): list with topics to extract,
          default=None (means all topics)
          remove_theta (bool): flag indicates save or remove Theta from model
          after extraction, default=False

        Returns:
          pandas.DataFrame: (data, columns, rows), where:
          1) columns --- the ids of documents, for which the Theta
          matrix was requested
          2) rows --- the names of topics in topic model, that was
          used to create Theta
          3) data --- content of Theta matrix
        """
        if self.cache_theta is False:
            raise ValueError('cache_theta == False. Set ARTM.cache_theta = True')
        if not self._initialized:
            raise RuntimeError("Model does not exist yet. Use " +
                               "ARTM.initialize()/ARTM.fit_*()")

        theta_info = self.master.get_theta_info(model=self.model)

        document_ids = [item_id for item_id in theta_info.item_id]
        all_topic_names = [topic_name for topic_name in theta_info.topic_name]
        use_topic_names = topic_names if topic_names is not None else all_topic_names
        nd_array = self.master.get_theta_matrix(model=self.model,
                                                topic_names=use_topic_names,
                                                clean_cache=remove_theta)

        theta_data_frame = DataFrame(data=nd_array.transpose(),
                                     columns=document_ids,
                                     index=use_topic_names)

        return theta_data_frame

    def transform(self, batches=None, collection_name=None, data_path='', data_format='batches'):
        """ARTM.transform() --- find Theta matrix for new documents

        Args:
          collection_name (str): the name of text collection (required if
          data_format == 'bow_uci'), default=None
          batches (list of str): list of file names of batches to be processed;
          if not None, than data_format should be 'batches'; format '*.batch',
          default=None
          data_path (str):
          1) if data_format == 'batches' =>
          folder containing batches and dictionary;
          2) if data_format == 'bow_uci' =>
          folder containing docword.txt and vocab.txt files;
          3) if data_format == 'vowpal_wabbit' => file in Vowpal Wabbit format;
          4) if data_format == 'plain_text' => file with text;
          default=''
          data_format (str): the type of input data;
          1) 'batches' --- the data in format of BigARTM;
          2) 'bow_uci' --- Bag-Of-Words in UCI format;
          3) 'vowpal_wabbit' --- Vowpal Wabbit format;
          4) 'plain_text' --- source text;
          default='batches'

        Returns:
          pandas.DataFrame: (data, columns, rows), where:
          1) columns --- the ids of documents, for which the Theta
          matrix was requested
          2) rows --- the names of topics in topic model, that was
          used to create Theta
          3) data --- content of Theta matrix.
        """
        if collection_name is None and data_format == 'bow_uci':
            raise IOError('No collection name was given')

        if not self._initialized:
            raise RuntimeError("Model does not exist yet. Use " +
                               "ARTM.initialize()/ARTM.fit_*()")

        target_folder = data_path
        if not data_format == 'batches':
            target_folder = tempfile.mkdtemp()
        try:
            batches_list = self._parse_collection_inline(target_folder=target_folder,
                                                         data_path=data_path,
                                                         data_format=data_format,
                                                         collection_name=collection_name,
                                                         batches=batches,
                                                         batch_size=1000)

            theta_info, nd_array = self.master.process_batches(
                        pwt=self.model,
                        batches=batches_list,
                        nwt='nwt_hat',
                        num_inner_iterations=self._num_document_passes,
                        class_ids=self._class_ids,
                        find_theta=True)

            document_ids = [item_id for item_id in theta_info.item_id]
            topic_names = [topic_name for topic_name in theta_info.topic_name]
            theta_data_frame = DataFrame(data=nd_array.transpose(),
                                         columns=document_ids,
                                         index=topic_names)

        finally:
            # Remove temp batches folder if necessary
            if not data_format == 'batches':
                shutil.rmtree(target_folder)

        return theta_data_frame

    def initialize(self, data_path=None, dictionary_name=None):
        """ARTM.initialize() --- initialize topic model before learning

        Args:
          data_path (str): name of directory containing BigARTM batches, default=None
          dictionary_name (str): the name of loaded BigARTM collection
          dictionary, default=None

        Note:
          Priority of initialization:
          1) batches in 'data_path'
          2) dictionary
        """
        if data_path is not None:
            self.master.initialize_model(model_name=self.model,
                                         disk_path=data_path,
                                         num_topics=self._num_topics,
                                         topic_names=self._topic_names,
                                         source_type='batches')
        else:
            self.master.initialize_model(model_name=self.model,
                                         dictionary_name=dictionary_name,
                                         num_topics=self._num_topics,
                                         topic_names=self._topic_names,
                                         source_type='dictionary')

        phi_info = self.master.get_phi_info(model=self.model)
        self._topic_names = [topic_name for topic_name in phi_info.topic_name]
        self._initialized = True

        # Remove all info about previous iterations
        self._score_tracker = {}
        self._synchronizations_processed = 0

def add_data_generator_arguments(parser):
    from preprocess import add_preprocess_arguments
    from src.basic.scenario_db import add_scenario_arguments
    from src.basic.dataset import add_dataset_arguments
    from retriever import add_retriever_arguments
    from src.basic.negotiation.price_tracker import add_price_tracker_arguments
    from src.basic.negotiation.slot_detector import add_slot_detector_arguments

    add_scenario_arguments(parser)
    add_preprocess_arguments(parser)
    add_dataset_arguments(parser)
    add_retriever_arguments(parser)
    add_price_tracker_arguments(parser)
    add_slot_detector_arguments(parser)

def get_data_generator(args, model_args, mappings, schema):
    from preprocess import DataGenerator, LMDataGenerator, EvalDataGenerator, Preprocessor
    from src.basic.scenario_db import ScenarioDB
    from src.basic.negotiation.price_tracker import PriceTracker
    from src.basic.negotiation.slot_detector import SlotDetector
    from src.basic.dataset import read_dataset, EvalExample
    from src.basic.util import read_json
    from retriever import Retriever

    # TODO: move this to dataset
    if args.eval:
        dataset = []
        for path in args.eval_examples_paths:
            dataset.extend([EvalExample.from_dict(schema, e) for e in read_json(path)])
    else:
        dataset = read_dataset(None, args)
    lexicon = PriceTracker(model_args.price_tracker_model)
    slot_detector = SlotDetector(slot_scores_path=model_args.slot_scores)

    # TODO: hacky
    if args.model == 'lm':
        DataGenerator = LMDataGenerator

    # Dataset
    if args.retrieve:
        retriever = Retriever(args.index, context_size=args.retriever_context_len, num_candidates=args.num_candidates)
    else:
        retriever = None

    preprocessor = Preprocessor(schema, lexicon, model_args.entity_encoding_form, model_args.entity_decoding_form, model_args.entity_target_form, slot_filling=model_args.slot_filling, slot_detector=slot_detector)

    if args.eval:
        data_generator = EvalDataGenerator(dataset, preprocessor, mappings, model_args.num_context)
    else:
        if args.test:
            model_args.dropout = 0
            train, dev, test = None, None, dataset.test_examples
        else:
            train, dev, test = dataset.train_examples, dataset.test_examples, None
        data_generator = DataGenerator(train, dev, test, preprocessor, schema, mappings, retriever=retriever, cache=args.cache, ignore_cache=args.ignore_cache, candidates_path=args.candidates_path, num_context=model_args.num_context)

    return data_generator

def add_model_arguments(parser):
    from src.model.encdec import add_basic_model_arguments
    from src.model.sequence_embedder import add_sequence_embedder_arguments
    from encdec import add_model_arguments
    from price_predictor import add_price_predictor_arguments
    from context_embedder import add_context_embedder_arguments
    from ranker import add_ranker_arguments

    add_basic_model_arguments(parser)
    add_model_arguments(parser)
    add_sequence_embedder_arguments(parser)
    add_price_predictor_arguments(parser)
    add_context_embedder_arguments(parser)
    add_ranker_arguments(parser)

def check_model_args(args):
    if args.pretrained_wordvec:
        with open(args.pretrained_wordvec, 'r') as fin:
            pretrained_word_embed_size = len(fin.readline().strip().split()) - 1
        assert pretrained_word_embed_size == args.word_embed_size

        if args.context and args.context_encoder == 'bow':
            assert pretrained_word_embed_size == args.context_size

    if args.slot_filling and args.test:
        assert args.batch_size == 1

    if args.decoder == 'rnn-attn':
        assert args.attention_memory is not None

    if args.num_context > 0:
        assert not args.stateful

    assert args.temperature >= 0

def build_model(schema, mappings, args):
    import tensorflow as tf
    from src.model.word_embedder import WordEmbedder
    from src.model.encdec import BasicEncoder, BasicDecoder, Sampler
    from price_predictor import PricePredictor
    from encdec import BasicEncoderDecoder, PriceDecoder, ContextDecoder, AttentionDecoder, LM, SlotFillingDecoder, ContextEncoder
    from ranker import IRRanker, CheatRanker, EncDecRanker, SlotFillingRanker
    from context_embedder import ContextEmbedder
    from preprocess import markers
    from src.model.sequence_embedder import get_sequence_embedder

    check_model_args(args)

    tf.reset_default_graph()
    tf.set_random_seed(args.random_seed)

    with tf.variable_scope('GlobalDropout'):
        if args.test:
            keep_prob = tf.constant(1.)
        else:
            # When test on dev set, we need to feed in keep_prob = 1.0
            keep_prob = tf.placeholder_with_default(tf.constant(1. - args.dropout), shape=[], name='keep_prob')

    vocab = mappings['vocab']
    pad = vocab.to_ind(markers.PAD)

    # Word embeddings
    word_embeddings = None
    context_word_embeddings = None
    if args.pretrained_wordvec is not None:
        word_embeddings = vocab.load_embeddings(args.pretrained_wordvec, args.word_embed_size)
        if args.context:
            context_word_embeddings = mappings['kb_vocab'].load_embeddings(args.pretrained_wordvec, args.word_embed_size)

    with tf.variable_scope('EncoderWordEmbedder'):
        encoder_word_embedder = WordEmbedder(vocab.size, args.word_embed_size, word_embeddings, pad)
    with tf.variable_scope('DecoderWordEmbedder'):
        decoder_word_embedder = WordEmbedder(vocab.size, args.word_embed_size, word_embeddings, pad)

    if args.decoding[0] == 'sample':
        sample_t = float(args.decoding[1])
        sampler = Sampler(sample_t)
    else:
        raise('Unknown decoding method')

    opts = vars(args)
    opts['vocab_size'] = vocab.size
    opts['keep_prob'] = keep_prob
    opts['embed_size'] = args.rnn_size
    encoder_seq_embedder = get_sequence_embedder(args.encoder, **opts)
    decoder_seq_embedder = get_sequence_embedder(args.decoder, **opts)

    if args.context is not None:
        context_opts = dict(opts)
        context_opts['vocab_size'] = mappings['kb_vocab'].size
        context_opts['embed_size'] = args.context_size

        with tf.variable_scope('ContextWordEmbedder'):
            context_word_embedder = WordEmbedder(context_opts['vocab_size'], context_opts['embed_size'], context_word_embeddings, pad=pad)

        with tf.variable_scope('CategoryWordEmbedder'):
            category_word_embedder = WordEmbedder(mappings['cat_vocab'].size, 10, pad=pad)
        context_seq_embedder = get_sequence_embedder(args.context_encoder, **context_opts)
        context_embedder = ContextEmbedder(mappings['cat_vocab'].size, context_word_embedder, category_word_embedder, context_seq_embedder, pad)

    def get_decoder(args):
        if args.decoder == 'rnn':
            if args.context is not None:
                decoder = ContextDecoder(decoder_word_embedder, decoder_seq_embedder, context_embedder, args.context, pad, keep_prob, vocab.size, sampler, args.sampled_loss, args.tied)
            else:
                decoder = BasicDecoder(decoder_word_embedder, decoder_seq_embedder, pad, keep_prob, vocab.size, sampler, args.sampled_loss, args.tied)
        else:
            decoder = AttentionDecoder(decoder_word_embedder, decoder_seq_embedder, pad, keep_prob, vocab.size, sampler, args.sampled_loss, context_embedder=context_embedder, attention_memory=args.attention_memory)

        if args.predict_price:
            price_predictor = PricePredictor(args.price_predictor_hidden_size, 1+2*args.price_hist_len)
            decoder = PriceDecoder(decoder, price_predictor)

        if args.slot_filling:
            decoder = SlotFillingDecoder(decoder)

        return decoder

    if args.model == 'encdec' or args.ranker == 'encdec':
        decoder = get_decoder(args)
        if args.num_context > 0:
            encoder = ContextEncoder(encoder_word_embedder, encoder_seq_embedder, args.num_context, pad, keep_prob)
        else:
            encoder = BasicEncoder(encoder_word_embedder, encoder_seq_embedder, pad, keep_prob)
        model = BasicEncoderDecoder(encoder, decoder, pad, keep_prob, stateful=args.stateful)
    elif args.model == 'lm':
        decoder = get_decoder(args)
        model = LM(decoder, pad)
    elif args.model is not None:
        raise ValueError('Unknown model')

    if args.ranker == 'cheat':
        model = CheatRanker()
    if args.ranker == 'ir':
        model = IRRanker()
    elif args.ranker == 'encdec':
        model = EncDecRanker(model, args.temperature)
    elif args.ranker == 'sf':
        model = SlotFillingRanker(model)

    return model


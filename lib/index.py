#!/usr/bin/python3
import os
import json
from math import log2, sqrt
from heapq import nlargest
from itertools import count
from collections import Counter
from lib.database import DataBase


# assigns ascending number to each individual input
class Numberer:

    def __init__(self, start = 0):
        self.known = dict()
        self.num_keys = start

    # get/set number for term
    def get(self, key):
        try:
            return self.known[key]
        except KeyError:
            self.num_keys += 1
            self.known[key] = self.num_keys
            return self.num_keys

    # remove known terms by value
    def remove_values(self, values):
        for key in list(self.known.keys()):
            if self.known[key] in values:
                del self.known[key]


class InvertedIndex:

    def __init__(self, database, documents, frequency_threshold):
        self.num_documents = 0
        self.database = database
        self.vocabulary_indices = Numberer()
        self.document_frequencies = Counter()
        self.document_ids = dict()

        infrequent = self.make_indices(documents = documents, frequency_threshold = frequency_threshold)
        self.remove_infrequent(infrequent)
        self.transform_to_tfidf()


    # takes an iterator with (doc_id, term_list)-tuples, creates:
    # an in-memory dictionary from terms to term_ids
    # an on-disk index
    # an in-memory dictionary from doc ids to document names (reddit comment ids)
    # an in-memory dictionary with document frequencies (i.e. the number of documents each term appears in)
    # returns list of term_ids below frequency threshold
    def make_indices(self, documents, frequency_threshold):
        vocabulary = Counter()
        self.prepare_inserts()
        for document in documents:
            self.process_document(document, vocabulary)
        infrequent = [term_id for term_id, freq in vocabulary.items() if freq < frequency_threshold]
        return infrequent


    # takes a document and the vocabulary seen so far
    # processes the document, adds it to the database
    # and updates the vocabulary, document_ids and
    # document_frequencies
    def process_document(self, document, vocabulary):
        doc_id = document[0]
        document = [self.vocabulary_indices.get(term) for term in document[1]]
        term_counts = Counter(document)
        vocabulary += term_counts
        for term in term_counts:
            self.document_frequencies[term] += 1
        self.insert_document(self.num_documents, list(term_counts.items()))
        self.document_ids[self.num_documents] = doc_id
        self.num_documents += 1

    # remove infrequent terms from database
    # and from vocabulary indices
    def remove_infrequent(self, infrequent):
        self.prepare_deletes()
        self.remove_terms([(term,) for term in infrequent])
        self.vocabulary_indices.remove_values(set(infrequent))


    # turns frequency counts in index into pmi values
    # temporarily creates reverse term_id dictionary
    # (will take a lot of memory if frequency threshold for vocabulary
    # is set very low and number of documents high)
    def transform_to_tfidf(self):
        self.prepare_updates()
        updates = list()
        for i, document_id in enumerate(self.document_ids):
            frequencies = self.get_document(document_id)
            tfidfs = [(term_id, self.tfidf(term_id, frequency)) for term_id, frequency in frequencies]
            norm = InvertedIndex.l2_norm([tfidf for _, tfidf in tfidfs])
            normed = [(tfidf/norm, document_id, term_id) for term_id, tfidf in tfidfs]
            updates += normed
            if i%10000 == 0:
                self.update_documents(updates)
                updates = list()
        if updates:
            self.update_documents(updates)

    # takes term_id and its frequency and returns
    # pmi value
    def tfidf(self, term_id, frequency):
        return frequency * self.idf(term_id)

    # returns idf score for a given term_id
    def idf(self, term_id):
        idf = log2(self.num_documents / max(self.document_frequencies[term_id],1))
        return idf

    # turns document id into name of document
    def get_document_name(self, doc_id):
        return self.document_ids[doc_id]

    # calculats l2 norm over a vector
    @staticmethod
    def l2_norm(values):
        l2_norm = sqrt(sum([value**2 for value in values]))
        return l2_norm

    # return term_id for given term
    def get_term_id(self, term):
        return self.vocabulary_indices.get(term)


    # interfaces for database
    # retrieves document_ids a given term_id appeared in
    def get_postings_list(self, term_id):
        return self.database.retrieve_term(term_id)

    # retrieves document from database by id
    def get_document(self, document_id):
        return self.database.retrieve_document(document_id)

    # insert document into database
    def insert_document(self, doc_id, scores):
        self.database.insert_document(doc_id, scores)

    # remove list of terms from database
    def remove_terms(self, infrequent):
        self.database.remove_terms(infrequent)

    def update_documents(self, updates):
        self.database.update_documents(updates)

    # prepare database for insertions
    def prepare_inserts(self):
        self.database.prepare_inserts()

    # prepare database for deletions
    def prepare_deletes(self):
        self.database.prepare_deletes()

    # prepare database for updates
    def prepare_updates(self):
        self.database.prepare_updates()


class QueryProcessor():

    def __init__(self, inverted_index):
        self.inverted_index = inverted_index

    def query_index(self, query, num_results):
        # ignore multiple occurrences of terms in query
        query = list(set(query.strip().split(" ")))
        term_ids = [self.get_term_id(term) for term in query]
        # get all documents containing any of the query terms
        candidates = set()
        for term_id in term_ids:
            doc_ids = self.get_postings_list(term_id)
            candidates.update(doc_ids)
        # get similarity between documents and query
        similarities = list()
        for candidate in candidates:
            similarities.append((self.get_similarity(candidate, term_ids), candidate))
        for i, term in enumerate(query):
            print("idf({0}): {1:2f}".format(term, self.get_idf(term_ids[i])))
        for similarity, doc_id in nlargest(num_results, similarities):
            doc_name = self.get_document_name(doc_id)
            print("{0} ({1:3f}): {2}".format(doc_id, similarity, doc_name))

    # return cosine similarity between doc_id and query (term ids)
    def get_similarity(self, candidate, query):
        query = self.query_to_tfidf(query)
        candidate = dict(self.get_document(candidate))
        cosine = 0
        for term_id, tf_idf in query:
            cosine += tf_idf * candidate.setdefault(term_id, 0)
        return cosine

    # turn query into vector of normed tf-ids scores
    def query_to_tfidf(self, query):
        query = [(term_id, self.tfidf(term_id, 1)) for term_id in query]
        l2_norm = QueryProcessor.l2_norm([tfidf for _, tfidf in query])
        query = [(term_id, tf_idf/l2_norm) for term_id, tf_idf in query]
        return query

    # calculats l2 norm over a vector
    @staticmethod
    def l2_norm(values):
        l2_norm = sqrt(sum([value**2 for value in values]))
        return l2_norm


    # interfaces to communicate with inverted_index
    # get idf for a term
    def get_idf(self, term):
        return self.inverted_index.idf(term)

    # get id of a term
    def get_term_id(self, term):
        return self.inverted_index.get_term_id(term)

    # get document ids term_id appears in
    def get_postings_list(self, term_id):
        return self.inverted_index.get_postings_list(term_id)

    # get name associated with document id
    def get_document_name(self, doc_id):
        return self.inverted_index.get_document_name(doc_id)

    # get term ids and tf-idf scores of terms in a document
    def get_document(self, doc_id):
        return self.inverted_index.get_document(doc_id)

    # calculate tf-idf from term id and frequency
    def tfidf(self, term_id, frequency):
        return self.inverted_index.tfidf(term_id, frequency)

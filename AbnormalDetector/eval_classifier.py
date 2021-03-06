'''
Reads a pcap, parses it into sessions, and classifies the sessions as being
either normal or abnormal using the specified model
'''

import sys
import os
import ast
import logging
from redis import StrictRedis
import numpy as np

from reader import sessionizer
from featurizer import is_private
from model_utils import clean_session_dict
from classifier import Classifier

import json
import pika

# Load parameters from config
with open('config.json') as config_file:
    config = json.load(config_file)
state_size = config['state size']
duration = config['duration']
look_time = config['look time']
threshold = config['threshold']

logging.basicConfig(level=logging.INFO)

def lookup_key(key):
    '''
    Look up a key from the input filename
    '''
    try:
        r = StrictRedis(host='redis', port=6379, db=0)
        key_info = r.hgetall(key)
        endpoint = key_info[b'endpoint']
        endpoint = endpoint.decode('utf-8')
        end_dict = ast.literal_eval(endpoint)
        address = end_dict['ip-address']
    except Exception as e:
        address = None
        return address, e

    return address, None

def get_address_info(address, timestamp):
    '''
    Look up address information prior to the timestamp
    '''
    # Get the timestamps of the past updates for this address
    try:
        r = StrictRedis(host='redis', port=6379, db=0)
        updates = r.hgetall(address)
        timestamps = json.loads(updates[b'timestamps'].decode('ascii'))
    except Exception as e:
        timestamps = None

    # Defaults if there are no previous updates
    current_state = np.zeros(state_size)
    average_state = np.zeros(state_size)
    other_ips = []

    # If there is a previous update, read out the state
    last_update = None
    if timestamps is not None:
        # Get the most recent update prior to the current timestsmp
        updates = [time for time in timestamps if time < timestamp]
        if len(updates) > 0:
            last_update = max(updates)

    # Read the state of the most recent update if there was one
    if last_update is not None:
        key = address + '_' + str(last_update)
        state = r.hgetall(key)

        current_state = json.loads(
                            state[b'current_representation'].decode('ascii')
                                  )
        average_state = json.loads(state[b'representation'].decode('ascii'))
        labels = ast.literal_eval(state[b'labels'].decode('utf-8'))
        confs = ast.literal_eval(state[b'confidences'].decode('utf-8'))
        other_ips = ast.literal_eval(state[b'other_ips'].decode('utf-8'))
    else:
        labels = None
        confs = None

    return current_state, average_state, other_ips, last_update, labels, confs

def basic_decision(
                    key,
                    address,
                    current_rep,
                    mean_rep,
                    prev_time,
                    timestamp,
                    labels,
                    confs
                  ):

    valid = True

    if labels is None:
        labels = ['Unknown']*3
        confs = [1,0,0]
        valid = False

    if key is None:
        key = address
        valid = False

    investigate = False
    if prev_time is None or timestamp - prev_time > look_time:
        investigate = True
    if labels[0] == 'Unknown':
        investigate = True

    behavior = 'normal'
    if np.dot(current_rep, mean_rep) < threshold:
        behavior = 'abnormal'

    output = {}
    decisions = {'behavior': behavior, 'investigate': investigate}
    classifications = {'labels': labels[0:3], 'confidences': confs[0:3]}
    id_dict = {
                'decisions': decisions,
                'classification': classifications,
                'timestamp': timestamp,
                'valid': valid
              }
    output[key] = id_dict
    return output

if __name__ == '__main__':
    logger = logging.getLogger(__name__)
    # Get the pcap path from the first argument
    pcap_path = sys.argv[1]
    # Determine the source key from filename
    split_path = os.path.split(pcap_path)[-1]
    split_path = split_path.split('.')
    split_path = split_path[0].split('-')
    key = split_path[0].split('_')[1]
    logger.info("looking up key address")
    key_address, e = lookup_key(key)
    logger.info("Looked up key address")
    logger.info(key_address)
    if e is not None:
        logger.info(e)

    # Get the source IP address
    if len(split_path) >= 7:
        source_ip = '.'.join(split_path[-4:])
    else:
        source_ip = None

    if key_address == source_ip:
        # Parse the pcap into sessions
        sessions, timestamp = sessionizer(pcap_path, duration=duration)

        # Clean the sessions
        cleaned_sessions = []
        inferred_ip = None
        for session_dict in sessions:
            cleaned_sessions, inferred_ip = \
                        clean_session_dict(session_dict, source_ip=source_ip)
        if source_ip is None:
            source_ip = inferred_ip

        # Make simple decisions based on vector differences and update times
        decisions = {}
        repr_s, m_repr_s, _ , prev_s, labels, confs = get_address_info(
                                                                     source_ip,
                                                                     timestamp
                                                                      )
        logger.info("Logging prev time")
        logger.info(prev_s)
        decision = basic_decision(
                                   key,
                                   source_ip,
                                   repr_s,
                                   m_repr_s,
                                   prev_s,
                                   timestamp,
                                   labels,
                                   confs
                                 )
        logger.info("Created message")
        logger.info(decision)


        # Get json message
        message = json.dumps(decision)
    
        # Get our "SKIP_RABBIT" environment variable with a default value of
        # false
        skip_rabbit = os.getenv("SKIP_RABBIT", False)

        # Convert our string into a boolean
        skip_rabbit = skip_rabbit.lower() in ["true", "t", "y", "1"]

        logger.debug("SKIP_RABBIT set to: " + skip_rabbit)
    
        if skip_rabbit:
            # Rabbit settings
            exchange = 'topic-poseidon-internal'
            exchange_type = 'topic'
    
            # Starting rabbit connection
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host='rabbit')
            )
    
            channel = connection.channel()
            channel.exchange_declare(
                exchange=exchange, exchange_type=exchange_type
            )
        
            routing_key = 'poseidon.algos.decider'
            channel.basic_publish(exchange=exchange,
                                  routing_key=routing_key,
                                  body=message)
            logger.debug("Routing key: " + routing_key)
            logger.debug("Exchange: " + exchange)
            connection.close()
        else:
            # Skipping rabbit. Printing to STDOUT
            logger.info("Message: " + message)

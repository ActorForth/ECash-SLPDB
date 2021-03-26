import json
import requests
import base64
import sys

from . import networks
from electroncash.slp_graph_search import slp_gs_mgr

DEFAULT_TIMEOUT = 30

def query_to_url(query, endpoint):
    query_to_string = json.dumps(query)
    query_b64 = base64.b64encode(query_to_string.encode("utf-8"))
    b64_to_str = str(query_b64)
    query_path = b64_to_str[2:-1]        
    url = endpoint + query_path
    return url

def query(txid, endpoint):
    query = {
        "v": 3,
        "q": {
            "db": ["c", "u"],
            "aggregate": [
            {
                "$match": {
                "tx.h": txid
                }
            },
            {
                "$limit": 1
            },
            {
                "$project": {
                "tx.h": "$tx.h",
                "slp.valid": "$slp.valid",
                "slp.invalidReason": "$slp.invalidReason"
                }
            }
            ],
            "limit": 1
        }
    }
    try:
        path = query_to_url(query, endpoint)
        result = requests.get(url=path, timeout=DEFAULT_TIMEOUT)
        json  = result.json()
    except:
        raise(Exception("Server was not reachable or something went wrong."))
    if json["c"]:
        return json["c"]
    if json["u"]:
        return json["u"]

def check_validity(txid):

    slpdb_endpoints = slp_gs_mgr.slpdb_host
    success_counter = 0
    for k in slpdb_endpoints:
        result = []
        try:
            result = query(txid, k)
            if result:
                if result[0]["slp"]["valid"]:
                    success_counter += 1
        except:
            continue
        
    return success_counter
        
        
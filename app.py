from flask import Flask, request, jsonify
import json
import os
import random
import string
from datetime import datetime

app = Flask(__name__)

KEY_FILE = "free_keys.json"


def load_keys():
    if not os.path.exists(KEY_FILE):
        return {}

    with open(KEY_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except:
            return {}


def save_keys(data):
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


@app.route("/")
def home():
    return "VB TOOL FREE SERVER OK"
    
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

from fastapi import FastAPI, Query
from pymongo import MongoClient
from typing import List
import json

# Initialize FastAPI
app = FastAPI()

# MongoDB connection
MONGO_URI = "mongodb://localhost:27017/"

client = MongoClient(MONGO_URI)

db = client["stock_data"]
collection = db["option_chain"]


@app.get("/get-option-chain")
def get_option_chain(timestamp: str = Query(...)):

    try:

        # Query MongoDB
        cursor = collection.find(
            {"timestamp": timestamp},
            {"_id": 0}   # Remove _id field
        )

        data = list(cursor)

        return {
            "status": "success",
            "timestamp": timestamp,
            "count": len(data),
            "data": data
        }

    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }
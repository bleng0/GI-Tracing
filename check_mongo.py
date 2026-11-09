"""Quick check that Python can reach the local MongoDB: python check_mongo.py"""
import os

import pymongo
from dotenv import load_dotenv

load_dotenv()

writer = pymongo.MongoClient(os.environ["MONGO_WRITE_URI"], serverSelectionTimeoutMS=3000).gi_trace
reader = pymongo.MongoClient(os.environ["MONGO_READ_URI"], serverSelectionTimeoutMS=3000).gi_trace

print("collections:", sorted(writer.list_collection_names()))
writer.certificates.insert_one({"_check": True})
print("writer insert ok, reader sees:", reader.certificates.count_documents({"_check": True}))
try:
    reader.certificates.insert_one({"_check": "should fail"})
    print("PROBLEM: reader was able to write")
except pymongo.errors.OperationFailure:
    print("reader write refused (correct)")
writer.certificates.delete_many({"_check": True})
print("cleaned up")

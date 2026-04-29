
import os
from pymongo import MongoClient
from dotenv import load_dotenv

def test_conn():
    load_dotenv(".env")
    uri = os.getenv("MONGODB_URI")
    db_name = os.getenv("MONGODB_DB_NAME")
    print(f"URI: {uri}")
    print(f"DB: {db_name}")
    
    client = MongoClient(uri)
    db = client[db_name]
    try:
        config = db.config.find_one({"key": "sync_analysis_config"})
        print(f"Config: {config}")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        client.close()

if __name__ == "__main__":
    test_conn()

import asyncio
from google import genai
client = genai.Client(api_key="test")
print(hasattr(client, "aio"))

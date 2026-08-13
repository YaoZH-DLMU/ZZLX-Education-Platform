# app/utils/encryption.py
from cryptography.fernet import Fernet
from functools import wraps
import os

class Encryptor:
    def __init__(self, key=None):
        self.key = key or os.environ.get('ENCRYPTION_KEY') or Fernet.generate_key()
        self.cipher = Fernet(self.key)
    
    def encrypt(self, data):
        if not data:
            return data
        return self.cipher.encrypt(str(data).encode()).decode()
    
    def decrypt(self, data):
        if not data:
            return data
        return self.cipher.decrypt(data.encode()).decode()
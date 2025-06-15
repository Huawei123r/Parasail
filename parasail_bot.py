import asyncio
import json
import os
import time
import requests
from web3 import Web3
from eth_account import Account, messages
from dotenv import load_dotenv # <-- New import

# --- Load Environment Variables ---
load_dotenv()

# --- Configuration Management ---
CONFIG_FILE = 'config.json'
BASE_URL = 'https://www.parasail.network/api'

class ParasailNodeBot:
    def __init__(self):
        # Load dynamic config (wallet_address, bearer_token) from config.json
        self.config = self._load_config()

        # Load private key from .env file
        self.private_key = os.getenv("PRIVATE_KEY")
        if not self.private_key:
            self.log("Error: PRIVATE_KEY not found in .env file.")
            self.log("Please ensure your .env file is configured correctly.")
            exit(1)

        self.w3 = Web3() # Web3 instance for signing

        # Initialize wallet with private key from .env
        try:
            self.wallet = Account.from_key(self.private_key)
            # Ensure wallet_address in config.json is consistent with the private key
            if self.config.get("wallet_address") != self.wallet.address:
                self.config["wallet_address"] = self.wallet.address
                self._save_config(self.config) # Save updated config
        except Exception as e:
            self.log(f"Error initializing wallet from PRIVATE_KEY: {e}")
            self.log("Please ensure PRIVATE_KEY in .env is correct and has no '0x' prefix.")
            exit(1)

        self.log(f"Wallet Address: {self.config.get('wallet_address')}")
        self.countdown_remaining_seconds = 0
        self.countdown_task = None
        self.stats_task = None

    def _load_config(self):
        try:
            if not os.path.exists(CONFIG_FILE):
                # Create an empty config if it doesn't exist
                initial_config = {
                    "wallet_address": "",
                    "bearer_token": ""
                }
                with open(CONFIG_FILE, 'w') as f:
                    json.dump(initial_config, f, indent=2)
                self.log(f"Created empty {CONFIG_FILE}.")
                # Don't exit here, as private key is loaded from .env
                return initial_config

            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            self.log(f"Error loading config from {CONFIG_FILE}: {e}")
            self.log("Please check if config.json is a valid JSON file.")
            exit(1)

    def _save_config(self, config):
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            self.log(f"Error saving config to {CONFIG_FILE}: {e}")

    def log(self, message):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"[{timestamp}] {message}")

    async def _send_api_request(self, method, endpoint, data=None, headers=None):
        url = f"{BASE_URL}{endpoint}"
        _headers = {"Content-Type": "application/json"}
        if self.config.get("bearer_token"):
            _headers["Authorization"] = f"Bearer {self.config['bearer_token']}"
        if headers:
            _headers.update(headers)

        try:
            if method == 'GET':
                response = requests.get(url, headers=_headers)
            elif method == 'POST':
                response = requests.post(url, headers=_headers, json=data)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            return response.json()
        except requests.exceptions.RequestException as e:
            self.log(f"API request to {endpoint} failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                self.log(f"Response status: {e.response.status_code}")
                self.log(f"Response body: {e.response.text}")
            raise

    async def generate_signature(self):
        # The message to be signed is hardcoded in the JS bot.
        message = (
            "By signing this message, you confirm that you agree to the Parasail Terms of Service.\n\n"
            "Parasail (including the Website and Parasail Smart Contracts) is not intended for:\n"
            "(a) access and/or use by Excluded Persons;\n"
            "(b) access and/or use by any person or entity in, or accessing or using the Website from, an Excluded Jurisdiction.\n\n"
            "Excluded Persons are prohibited from accessing and/or using..."
        )
        # Using eth_account.messages.encode_defunct for personal_sign
        encoded_message = messages.encode_defunct(text=message)
        signed_message = self.w3.eth.account.sign_message(encoded_message, private_key=self.private_key)
        return signed_message.signature.hex()

    async def verify_user(self):
        self.log("Verifying user and obtaining bearer token...")
        try:
            signature = await self.generate_signature()
            data = {
                "wallet_address": self.config["wallet_address"],
                "signature": signature
            }
            response = await self._send_api_request('POST', '/auth/verify', data)
            self.config["bearer_token"] = response.get("bearer_token")
            self._save_config(self.config)
            self.log("User verified. Bearer token obtained and saved.")
            return True
        except Exception as e:
            self.log(f"User verification failed: {e}")
            return False

    async def onboard_node(self):
        self.log("Attempting to onboard node...")
        try:
            response = await self._send_api_request('POST', '/node/onboard', {})
            self.log(f"Node onboarded successfully: {response.get('message', 'No message')}")
        except Exception as e:
            self.log(f"Node onboarding failed: {e}")

    async def check_in(self):
        self.log("Performing node check-in (power clicker equivalent)...")
        try:
            response = await self._send_api_request('POST', '/node/checkin', {})
            self.log(f"Node checked in successfully. Points: {response.get('points', 'N/A')}, Message: {response.get('message', 'No message')}")
        except Exception as e:
            self.log(f"Node check-in failed: {e}")

    async def get_node_stats(self):
        self.log("Fetching node stats...")
        try:
            stats = await self._send_api_request('GET', '/node/stats')
            self.log(f"Node Stats - Tier: {stats.get('tier', 'N/A')}, Status: {stats.get('status', 'N/A')}, Total Points: {stats.get('total_points', 'N/A')}, Streak: {stats.get('streak', 'N/A')}")
            self.log(f"Last Check-in: {stats.get('last_checkin', 'N/A')}")
            return stats
        except Exception as e:
            self.log(f"Failed to fetch node stats: {e}")
            return {}

    async def start_countdown(self):
        if self.countdown_task:
            self.countdown_task.cancel()
        if self.stats_task:
            self.stats_task.cancel()

        initial_stats = await self.get_node_stats()
        if 'next_checkin_timestamp' in initial_stats and initial_stats['next_checkin_timestamp'] is not None:
            next_checkin_ms = initial_stats['next_checkin_timestamp']
            current_ms = time.time() * 1000
            self.countdown_remaining_seconds = max(0, int((next_checkin_ms - current_ms) / 1000))
            self.log(f"Next routine tasks in {self.countdown_remaining_seconds} seconds (approx. {self.countdown_remaining_seconds / 3600:.2f} hours).")
        else:
            self.log("Could not determine next check-in time from stats. Scheduling in 12 hours (fallback).")
            self.countdown_remaining_seconds = 12 * 3600 # Fallback to 12 hours

        self.countdown_task = asyncio.create_task(self._countdown_loop())
        self.stats_task = asyncio.create_task(self._stats_update_loop())


    async def _countdown_loop(self):
        while self.countdown_remaining_seconds > 0:
            await asyncio.sleep(min(self.countdown_remaining_seconds, 60))
            self.countdown_remaining_seconds -= min(self.countdown_remaining_seconds, 60)
        
        self.log('Time to perform routine tasks!')
        await self.perform_routine_tasks()


    async def _stats_update_loop(self):
        while True:
            await asyncio.sleep(60) # Update every 60 seconds
            try:
                await self.get_node_stats()
            except Exception as e:
                self.log(f"Stats update loop failed: {e}")

    async def perform_routine_tasks(self):
        self.log("Performing routine tasks (onboard, check-in)...")
        try:
            await self.onboard_node()
            await asyncio.sleep(2)
            await self.check_in()
            await asyncio.sleep(2)
            await self.start_countdown() # Schedule next routine
        except Exception as e:
            self.log(f"Routine tasks failed: {e}")

    async def start(self):
        self.log("Starting Parasail Node Bot (Python equivalent)")
        try:
            if not self.config.get("bearer_token"):
                self.log("No bearer token found. Attempting to verify user...")
                success = await self.verify_user()
                if not success:
                    self.log("Failed to obtain bearer token. Please check private key and network connection.")
                    return # Exit if we can't get token

            # Initial tasks after ensuring token
            await self.onboard_node()
            await asyncio.sleep(2)
            await self.check_in()
            await asyncio.sleep(2)

            await self.start_countdown() # Start the main operational loop

            # Keep the bot running
            while True:
                await asyncio.sleep(3600) # Sleep for a long time, tasks are scheduled independently

        except Exception as e:
            self.log(f"Initialization failed: {e}")

async def main():
    bot = ParasailNodeBot()
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main()) 

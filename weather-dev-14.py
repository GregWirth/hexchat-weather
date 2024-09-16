import asyncio
import aiohttp
import logging
import time
import random
import signal
import json
import sys  # Added for sys.exit
from collections import defaultdict, deque
from contextlib import suppress
from urllib.parse import quote
import argparse
import logging.handlers
from cachetools import TTLCache  # Added for TTLCache

# Configure logging with adjustable levels and log rotation
parser = argparse.ArgumentParser(description='IRC Weather Bot')
parser.add_argument('--log-level', default='INFO', help='Set the logging level (DEBUG, INFO, WARNING, ERROR)')
parser.add_argument('--config', default='config.json', help='Path to the configuration file')  # Allow custom config file
args = parser.parse_args()

logger = logging.getLogger('IrcBot')
logger.setLevel(getattr(logging, args.log_level.upper()))
handler = logging.handlers.TimedRotatingFileHandler('bot.log', when='midnight', backupCount=7)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Load configuration from config.json or specified config file
with open(args.config, 'r') as config_file:
    config = json.load(config_file)

# Validate required configurations
REQUIRED_CONFIG_KEYS = ['HOST', 'PORT', 'USER', 'CHANNELS', 'API_KEY']
for key in REQUIRED_CONFIG_KEYS:
    if key not in config:
        raise ValueError(f"Missing required configuration: {key}")

HOST = config['HOST']
PORT = config['PORT']
USER = config['USER']
CHANNELS = config['CHANNELS']
TRIGGER = config['TRIGGER']
RATE_LIMIT = config['RATE_LIMIT']
RATE_LIMIT_TIME = config['RATE_LIMIT_TIME']
IGNORE_TIME = config['IGNORE_TIME']
WAREZ_TRIGGER = config['WAREZ_TRIGGER']
WAREZ_FILE = config['WAREZ_FILE']
PING_INTERVAL = config['PING_INTERVAL']
PING_TIMEOUT = config['PING_TIMEOUT']

API_KEY = config['API_KEY']  # Assuming API_KEY is in config

# Load ADMIN_USERS from config
ADMIN_USERS = config.get('ADMIN_USERS', [])

class IrcBot:
    """An IRC bot that provides weather information and responds to specific triggers."""

    def __init__(self):
        self.last_requests = defaultdict(lambda: deque(maxlen=RATE_LIMIT))
        self.global_request_times = deque(maxlen=RATE_LIMIT)
        self.warez_responder = WarezResponder(WAREZ_FILE)
        self.last_pong_time = time.time()
        self.reader = None
        self.writer = None
        self.lock = asyncio.Lock()
        self.tasks = []
        self.weather_cache = TTLCache(maxsize=100, ttl=300)  # Cache up to 100 items for 5 minutes
        self.running = True

    async def connect(self):
        """Establish a connection to the IRC server and register the bot."""
        try:
            self.reader, self.writer = await asyncio.open_connection(HOST, PORT)
            await self.register()
            await self.join_channels()
            logger.info("Connected to IRC server.")
        except asyncio.TimeoutError:
            logger.error(f"Timed out while trying to connect to IRC server at {HOST}:{PORT}")
            raise
        except Exception as e:
            logger.error(f"Failed to connect to IRC: {e}")
            raise

    async def register(self):
        """Register the bot with the IRC server."""
        self.writer.write(f"NICK {USER}\r\n".encode())
        self.writer.write(f"USER {USER} 0 * :{USER}\r\n".encode())
        await self.writer.drain()

    async def join_channels(self):
        """Join the specified IRC channels."""
        for channel in CHANNELS:
            self.writer.write(f"JOIN {channel}\r\n".encode())
            await self.writer.drain()

    async def ping_server(self):
        """Periodically send PING messages to the server to keep the connection alive."""
        try:
            while self.running:
                self.writer.write(f"PING :{HOST}\r\n".encode())
                await self.writer.drain()
                await asyncio.sleep(PING_INTERVAL)
        except asyncio.CancelledError:
            logger.info("Ping task cancelled.")
        except Exception as e:
            logger.error(f"Ping failed: {e}")

    async def handle_messages(self):
        """Handle incoming messages from the IRC server."""
        try:
            while self.running:
                line = await self.reader.readline()
                if not line:
                    raise ConnectionError("Connection lost.")
                line = line.decode('utf-8', errors='ignore').strip()
                logger.debug(f"Received line: {line}")
                await self.process_line(line)
        except (ConnectionError, asyncio.IncompleteReadError) as e:
            logger.error(f"Connection error: {e}")
            await self.cleanup()
            await self.reconnect()
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            await self.cleanup()
            await self.reconnect()

    def parse_irc_message(self, message):
        """Parse an IRC message into its prefix, command, and parameters."""
        prefix = ''
        trailing = []
        if not message:
            return None, None, None
        if message.startswith(':'):
            prefix, message = message[1:].split(' ', 1)
        if ' :' in message:
            message, trailing = message.split(' :', 1)
            args = message.split()
            args.append(trailing)
        else:
            args = message.split()
        command = args.pop(0)
        return prefix, command, args

    async def process_line(self, line):
        """Process a single line from the IRC server."""
        prefix, command, params = self.parse_irc_message(line)
        if command == 'PING':
            await self.handle_ping(params)
        elif command == 'PRIVMSG':
            await self.handle_privmsg(prefix, params)
        elif command == 'ERROR':
            logger.error(f"Server error: {' '.join(params)}")
            await self.reconnect()
        elif command in ('NOTICE', '001', '002', '003', '004', '375', '372', '376'):
            # Handle notices and welcome messages
            pass
        else:
            logger.debug(f"Unhandled message: {line}")

    async def handle_ping(self, params):
        """Respond to server PING messages."""
        self.writer.write(f"PONG :{params[0]}\r\n".encode())
        await self.writer.drain()
        self.last_pong_time = time.time()
        logger.debug("Responded to PING with PONG.")

    async def handle_privmsg(self, prefix, params):
        """Handle PRIVMSG commands."""
        user = prefix.split('!')[0]
        channel = params[0]
        message = params[1]

        if channel == USER:
            # This is a private message to the bot
            await self.handle_private_message(user, message)
        else:
            # Message in a channel
            if message.startswith(TRIGGER):
                location = message[len(TRIGGER):].strip()
                await self.handle_weather_command(user, channel, location)
            elif WAREZ_TRIGGER in message:
                await self.handle_warez_command(channel)

    async def handle_private_message(self, user, message):
        """Handle private messages sent to the bot."""
        if user not in ADMIN_USERS:
            logger.warning(f"Unauthorized user {user} attempted to use admin command.")
            await self.send_notice(user, "You are not authorized to use this command.")
            return

        if message.startswith('.say '):
            parts = message.split(' ', 2)
            if len(parts) < 3:
                await self.send_notice(user, "Usage: .say #channel message")
                return
            target_channel = parts[1]
            say_message = parts[2]
            await self.send_message(target_channel, say_message)
            logger.info(f"Admin {user} sent message to {target_channel}: {say_message}")
            await self.send_notice(user, f"Message sent to {target_channel}.")
        else:
            # Handle other private commands if needed
            await self.send_notice(user, "Unknown command.")

    async def send_notice(self, user, message):
        """Send a notice to a user."""
        self.writer.write(f"NOTICE {user} :{message}\r\n".encode())
        await self.writer.drain()

    async def handle_weather_command(self, user, channel, location):
        """Process the weather command and send weather information."""
        current_time = time.time()
        async with self.lock:
            # Global rate limit
            while self.global_request_times and current_time - self.global_request_times[0] > RATE_LIMIT_TIME:
                self.global_request_times.popleft()
            if len(self.global_request_times) >= RATE_LIMIT:
                warning_msg = f"The bot is currently rate limited due to high usage. Please try again later."
                await self.send_message(channel, warning_msg)
                logger.warning("Global rate limit exceeded.")
                return
            self.global_request_times.append(current_time)
            # Per-user rate limit
            request_times = self.last_requests[user]
            while request_times and current_time - request_times[0] > RATE_LIMIT_TIME:
                request_times.popleft()
            if len(request_times) >= RATE_LIMIT:
                warning_msg = f"You are being rate limited, {user}."
                await self.send_message(channel, warning_msg)
                logger.warning(f"Rate limit exceeded for user {user}.")
                return
            request_times.append(current_time)
        await self.fetch_and_send_weather(channel, location, user)

    async def fetch_and_send_weather(self, channel, location, user):
        """Fetch weather data and send it to the channel."""
        cache_key = location.lower()
        if cache_key in self.weather_cache:
            data = self.weather_cache[cache_key]
            logger.info(f"Using cached weather data for {location}.")
        else:
            try:
                url = f"https://api.weatherapi.com/v1/forecast.json?key={API_KEY}&q={quote(location)}&days=1&aqi=no&alerts=no"
                timeout = aiohttp.ClientTimeout(total=10)
                # Use async context manager for session
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            raise aiohttp.ClientError(f"HTTP error {resp.status}")
                        data = await resp.json()
                self.weather_cache[cache_key] = data
            except asyncio.TimeoutError:
                logger.error(f"Weather API request for {location} timed out.")
                error_msg = "Weather API request timed out."
                await self.send_message(channel, error_msg)
                return
            except Exception as e:
                logger.error(f"Error fetching weather data for {location}: {e}")
                error_msg = f"Error fetching weather information for {location}."
                await self.send_message(channel, error_msg)
                return

        # Extract and format the weather data
        try:
            location_info = data['location']
            current = data['current']
            forecast_day = data['forecast']['forecastday'][0]
            forecast = forecast_day['day']
            astro = forecast_day['astro']

            # Extract all the required fields
            name = location_info['name']
            region = location_info['region']
            country = location_info['country']
            temp_f = current['temp_f']
            temp_c = current['temp_c']
            condition_text = current['condition']['text']
            wind_mph = current['wind_mph']
            wind_kph = current['wind_kph']
            wind_degree = current['wind_degree']
            wind_dir = current['wind_dir']
            gust_mph = current['gust_mph']
            gust_kph = current['gust_kph']
            precip_mm = current['precip_mm']
            precip_in = current['precip_in']
            humidity = current['humidity']

            mintemp_f = forecast['mintemp_f']
            mintemp_c = forecast['mintemp_c']
            maxtemp_f = forecast['maxtemp_f']
            maxtemp_c = forecast['maxtemp_c']
            maxwind_mph = forecast['maxwind_mph']
            maxwind_kph = forecast['maxwind_kph']
            daily_chance_of_rain = forecast.get('daily_chance_of_rain', 0)
            daily_chance_of_snow = forecast.get('daily_chance_of_snow', 0)
            totalsnow_cm = forecast.get('totalsnow_cm', 0)
            try:
                totalsnow_cm = float(totalsnow_cm)
            except (ValueError, TypeError):
                totalsnow_cm = 0.0

            if totalsnow_cm > 0:
                totalsnow_in = totalsnow_cm / 2.54  # Convert cm to inches
                snow_message = f"Total Snow: {totalsnow_cm} cm / {totalsnow_in:.2f} in"
            else:
                snow_message = "Total Snow: 0 cm / 0 in"

            moon_phase = astro['moon_phase']
            sunrise = astro['sunrise']
            sunset = astro['sunset']

            weather_message = (
                f"{name}, {region}, {country} | "
                f"Current Temp: {temp_f}°F / {temp_c}°C | "
                f"Min Temp: {mintemp_f}°F / {mintemp_c}°C | "
                f"Max Temp: {maxtemp_f}°F / {maxtemp_c}°C | "
                f"Condition: {condition_text} | "
                f"Humidity: {humidity}% | "
                f"Wind: {wind_mph} mph / {wind_kph} kph "
                f"({wind_degree}°, {wind_dir}) | "
                f"Gusts: {gust_mph} mph / {gust_kph} kph | "
                f"Precipitation: {precip_in} in / {precip_mm} mm | "
                f"Moon Phase: {moon_phase} | "
                f"Sunrise: {sunrise} | Sunset: {sunset} | "
                f"Chance of Rain: {daily_chance_of_rain}% | "
                f"Chance of Snow: {daily_chance_of_snow}% | "
                f"{snow_message}"
            )

            await self.send_message(channel, weather_message)
            logger.info(f"Sent weather info to {channel} for location '{location}' requested by user '{user}'.")
        except KeyError as e:
            logger.error(f"Missing expected data in API response for {location}: {e}")
            error_msg = "Received unexpected data from weather API."
            await self.send_message(channel, error_msg)
        except Exception as e:
            logger.error(f"Error processing weather data for {location}: {e}")
            error_msg = f"Error processing weather information for {location}."
            await self.send_message(channel, error_msg)

    async def handle_warez_command(self, channel):
        """Respond to the warez trigger."""
        response = self.warez_responder.get_random_response()
        await self.send_message(channel, response)
        logger.info(f"Sent warez response to {channel}.")

    async def send_message(self, channel, message):
        """Send a message to the IRC channel, splitting if too long."""
        max_length = 400  # Reserve space for protocol overhead
        # Split the message into chunks if it exceeds the max length
        while len(message) > max_length:
            part = message[:max_length]
            self.writer.write(f"PRIVMSG {channel} :{part}\r\n".encode())
            await self.writer.drain()
            message = message[max_length:]
        self.writer.write(f"PRIVMSG {channel} :{message}\r\n".encode())
        await self.writer.drain()

    async def run(self):
        """Run the bot."""
        while self.running:
            try:
                await self.connect()
                # Start the ping task to keep the connection alive
                ping_task = asyncio.create_task(self.ping_server())
                self.tasks.append(ping_task)
                await self.handle_messages()
            except Exception as e:
                logger.error(f"Unhandled exception: {e}")
                await self.reconnect()
            finally:
                await self.cleanup()

    async def reconnect(self):
        """Attempt to reconnect to the IRC server with exponential backoff."""
        retry_delay = 5  # Start with a 5-second delay
        retries = 0
        max_retries = 10
        while self.running and retries < max_retries:
            try:
                logger.info("Attempting to reconnect...")
                await self.connect()
                # Restart the ping task
                ping_task = asyncio.create_task(self.ping_server())
                self.tasks.append(ping_task)
                # If connection is successful, break out of the loop
                await self.handle_messages()
                break
            except Exception as e:
                logger.error(f"Reconnection failed: {e}")
                retries += 1
                if retries >= max_retries:
                    logger.error(f"Max reconnection attempts ({max_retries}) reached. Exiting.")
                    self.running = False
                    break
                logger.info(f"Retrying in {retry_delay} seconds (Attempt {retries}/{max_retries})...")
                await asyncio.sleep(retry_delay)
                # Increase the delay exponentially, up to a maximum of 5 minutes
                retry_delay = min(retry_delay * 2, 300)

    async def cleanup(self):
        """Clean up resources on shutdown."""
        self.running = False
        for task in self.tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self.writer:
            self.writer.write("QUIT :Shutting down...\r\n".encode())
            await self.writer.drain()
            self.writer.close()
            await self.writer.wait_closed()
        logger.info("Cleaned up resources.")

class WarezResponder:
    """Responds with random messages from a predefined list when triggered."""

    def __init__(self, file_path):
        try:
            with open(file_path, 'r') as file:
                self.responses = [line.strip() for line in file if line.strip()]
        except FileNotFoundError:
            self.responses = ["No warez responses available."]

    def get_random_response(self):
        """Get a random response from the list."""
        return random.choice(self.responses) if self.responses else "No warez responses available."

if __name__ == "__main__":
    bot = IrcBot()

    async def main():
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.cleanup()))
        await bot.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully.")
        sys.exit(0)


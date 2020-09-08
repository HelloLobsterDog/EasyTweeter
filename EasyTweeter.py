import configparser
import codecs
import time
import os
import os.path
import sys
import logging
import logging.handlers

import tweepy


__version__ = '0.9.1'


class EasyTweeterException(RuntimeError):
	''' Application-specific exception used to wrap errors for the bot class. '''
	
class RateLimitRetriesExceeded(EasyTweeterException):
	''' exception thrown when the rate limit for API calls has been exceeded, and the retries have been exhausted '''
	
class MissingPermissionsError(EasyTweeterException):
	''' exception thrown when the api connection failed due to API error code 403s '''


class EasyTweeter():
	'''
	This class aims to provide a simple interface to twitter, for bots which conform to the following use case:
		-The bot posts one simple tweet (text provided by your client code) every time you run it, and then exits.
		-When the bot posts, it should run in the background and not require manual intervention in any form, because we're running on a schedule.
		-When things of interest occur, such as twitter users replying to the bot, or following it, or retweeting it, these interactions should be logged 
			and/or handled via hook methods which you can override. Also, you're ok with the bot checking for interaction only when it's scheduled run happens.
			This means, for example, that the bot won't be replying to users moments after they send the bot something.
		-When errors occur, they should be logged, and the bot will exit gracefully (since we're supposed to be running in the background and not interrupt).
			Unless those errors are due to rate limiting, in which case we can safely handle it by waiting a bit and retrying.
		-The bot maintainer will watch (or automatically monitor) the logs to find out if there are errors or anything of interest happening.
	
	Unless you want to implement some additional features yourself via the hook methods, you should also want the following:
		-The bot does not interact with users, or even reply to its own posts, it just posts one message every time you run it.
		-The bot doesn't follow alot of people. (This makes getting replies to the bot's posts more reliable due to a restriction of twitter's api)
	'''
	CHARACTER_LIMIT = 280
	
	def __init__(self, configurationDirectory = None, secondsSleepWhenRateLimited = 960, rateLimitRetries = 2, useRootLogger = False, logger = None):
		'''
		configurationDirectory is the directory in which all files/directories read from and written to by the bot reside, including logs (in a "logs"
		subdirectory by default), credentials, and files in which the bot stores it's state between runs (in a "state" subdirectory by default).
		By default, this is a directory "EasyTweeter" in the current directory.
		If the API connection hits the API call limit, it will sleep [secondsSleepWhenRateLimited] seconds before retrying. Defaults to 16 minutes.
		If the API call limit is hit, it will sleep and retry [rateLimitRetries] times before giving up.
		If useRootLogger is true, the bot will configure and use the root logger, and all logging messages (for instance, messages from urllib) will show up in the logs.
		If logger is provided, all logging will use the provided logger. If not provided, a default logger will be created, using a 
		TimedRotatingFileHandler which rotates at midnight, as well as logging to the console.
		'''
		if logger == None:
			self.logger = self._makeLogger(useRootLogger)
		else:
			self.logger = logger
		
		self.sleep = secondsSleepWhenRateLimited
		if self.sleep < 0:
			self.sleep = 0
		
		self.retries = rateLimitRetries
		if self.retries < 1:
			self.retries = 1
			
		self.configurationDirectory = configurationDirectory
		if self.configurationDirectory == None:
			self.configurationDirectory = os.path.join('.', 'EasyTweeter')
		
		self.api = None
		
	###############################################################################################################
	# these methods exist to allow you to override them to modify or hook into the bot's functionality:
	def getCredentialsConfigFilename(self):
		'''
		Returns a filename pointing to an ini file containing credentials, which we'll use to authenticate to twitter.
		This filename is only used if we attempt to tweet or check statuses without authenticating beforehand.
		Can be overridden by subclasses if you wish to change the path.
		'''
		return os.path.join(self.configurationDirectory, 'credentials.ini')
		
	def getLogFilename(self):
		'''
		Returns the log file name to use.
		By default, it is "logs/EasyTweeter.log" within the configuration directory from our constructor.
		Can be overridden by subclasses if you wish to change the path.
		'''
		return os.path.join(self.configurationDirectory, 'logs', 'EasyTweeter.log')
		
	def getStateDirectory(self):
		'''
		Return the path to the directory in which the class will store its state.
		By default, it is placed in the directory "state" within the configuration directory from our constructor.
		Can be overridden by subclasses if you wish to change the path.
		'''
		return os.path.join(self.configurationDirectory, 'state')
	
	def handleTweet(self, tweet):
		'''
		This method is called after the bot successfully tweets, and is passed the tweet which was created.
		It exists so subclasses can override it to change it's behavior or provide new functionality.
		By default, it doesn't do anything.
		'''
		pass
	
	def handleNewFollower(self, follower):
		'''
		This method is called when a new follower is found, and is passed the follower's @ handle.
		It exists so subclasses can override it to change it's behavior or provide new functionality.
		By default, all it does is log the follower.
		'''
		self.logger.info('[NEW] New follower: ' + str(follower.name) + " (@" + str(follower.screen_name) + ") - id:" + str(follower.id) + ' --- https://twitter.com/' + str(follower.screen_name))
	
	def handleFavorite(self, favoritedTweet, previousNumFavorites):
		'''
		This method is called when a status the bot posted has received a new favorite, and is passed the tweet which was favorited,
		and it's previous number of favorites, before we checked.
		It exists so subclasses can override it to change it's behavior or provide new functionality.
		By default, all it does is log the favorite.
		'''
		self.logger.info('[NEW] message ' + self._getLinkToStatus(favoritedTweet) + " has " + str(favoritedTweet.favorite_count) + " favorites, which changed from " + str(previousNumFavorites))
	
	def handleReply(self, reply):
		'''
		This method is called when the bot receives a reply to one of it's messages, and is passed the reply tweet.
		It exists so subclasses can override it to change it's behavior or provide new functionality.
		By default, all it does is log the reply.
		'''
		self.logger.info("[NEW] Reply from @" + reply.author.screen_name + " - " + self._getLinkToStatus(reply) + " - Text: " + reply.text)
	
	def handleRetweet(self, retweet):
		'''
		This method is called when a status the bot made was retweeted, and is passed the tweet which was retweeted.
		It exists so subclasses can override it to change it's behavior or provide new functionality.
		By default, all it does is log the retweet.
		'''
		self.logger.info('[NEW] One or more new retweets on tweet: ' + self._getLinkToStatus(retweet) + " it has been retweeted " + str(retweet.retweet_count) + ' times.')
	
	def handleDM(self, msg):
		'''
		This method is called after the bot receives a direct message, and is passed the DM.
		It exists so subclasses can override it to change it's behavior or provide new functionality.
		By default, all it does is log the DM.
		'''
		self.logger.info("[NEW] direct message from user @" + msg.author.screen_name + " - Text: " + msg.text)
		
	###############################################################################################################
	# helper methods (not part of the external interface, hence the _ prefix):
	def _makeLogger(self, makeRootLogger):
		''' Creates the default logger which is used by the class if one is not provided in init. '''
		if makeRootLogger:
			logger = logging.getLogger()
		else:
			logger = logging.getLogger('EasyTweeter')
		logger.setLevel(logging.INFO)
		# file
		fileHandler = logging.handlers.TimedRotatingFileHandler(self.getLogFilename(), when = 'midnight', encoding = 'utf-8')
		fileHandler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)-5s] %(name)s: %(message)s'))
		logger.addHandler(fileHandler)
		# console
		consoleHandler = logging.StreamHandler()
		consoleHandler.setFormatter(logging.Formatter('[%(levelname)s] %(name)s: %(message)s'))
		logger.addHandler(consoleHandler)
		return logger
		
	def _initState(self):
		'''
		If it does not exist, this method creates the directory in which the class will store its state.
		The directory which is created is returned by the method getStateDirectory(), which can be overridden to change it.
		'''
		if not os.path.exists(self.getStateDirectory()):
			self.logger.debug('creating state directory: ' + str(self.getStateDirectory()))
			os.makedirs(self.getStateDirectory())
			
	def _getLinkToStatus(self, status):
		''' utility method which takes a status object, and returns a string containing a link which can be used to view the status. '''
		return "https://twitter.com/" + str(status.author.screen_name) + "/status/" + str(status.id)
		
	def _shouldCheckUpdates(self, runInterval):
		'''
		This method takes the number of runs in between checks for updates, reads the state file
		containing what run we're on, determines whether we should check or not based on its
		content, and updates the file again. Returns whether or not we should check for the updates.
		'''
		# try opening up the config file to see if we should run or not
		stateFilePath = os.path.join(self.getStateDirectory(), 'UpdateCheckInterval.ini')
		config = configparser.ConfigParser()
		if os.path.exists(stateFilePath):
			self.logger.debug('reading update check interval file: ' + str(stateFilePath))
			config.read_file(codecs.open(stateFilePath, 'r', 'utf8'))
		else:
			self.logger.debug('No update check interval file')
		
		# does the file contain the right values?
		if 'Update Check Interval' in config:
			if 'last' in config['Update Check Interval']:
				loaded = config['Update Check Interval']['last']
				# yes. What is it?
				try:
					loaded = int(loaded)
					self.logger.info("Read and loaded update check interval value out of config file: " + str(loaded))
				except ValueError:
					self.logger.error("update check interval file exists, but contained invalid data.")
					loaded = 0
			else:
				self.logger.error("update check interval file exists, but contained incomplete data.")
				loaded = 0
		else:
			loaded = 0
		
		# should we do the checks?
		if loaded > 0:
			toRun = False
			toSave = loaded - 1
			self.logger.info('update check interval not hit. Saving value for next time: ' + str(toSave))
		else:
			toRun = True
			toSave = runInterval
			self.logger.info('update check interval was hit, so updates will be checked. Saving value for next time: ' + str(toSave))
		
		# save off the config now that we're done.
		config['Update Check Interval'] = {}
		config['Update Check Interval']['last'] = str(toSave)
		self._initState()
		with open(stateFilePath, 'w') as stateFile:
				config.write(stateFile)
			
		return toRun
		
	###############################################################################################################
	# the "important" external interface methods:
	def connect(self, consumerKey, consumerSecret, accessToken, accessTokenSecret): # if you want to pass your credentials manually
		'''
		Establishes a twitter API connection given the credentials provided.
		One of the variations of this function needs to be called before any of the other methods of this class.
		Raises a ValueError if the credentials are not valid.
		'''
		auth = tweepy.OAuthHandler(consumerKey, consumerSecret)  
		auth.set_access_token(accessToken, accessTokenSecret)  
		api = tweepy.API(auth)
		if not api.verify_credentials():
			raise ValueError('credentials provided were not valid')
		self.logger.info('Twitter API connection established.')
		self.api = api
			
	def connectFromConfig(self, credentialConfigPath): # typical authentication method
		'''
		Reads the credentials out of the config file at the path provided, and establishes 
		a twitter API connection. Raises a ValueError if the credentials are not valid.
		
		The file should be a UTF-8 encoded ini file, formatted as this example:
			[TwitterCredentials]
			ConsumerKey = xxxxxxxxxx
			ConsumerSecret = xxxxxxxxxx
			AccessToken = xxxxxxxxxx
			AccessTokenSecret = xxxxxxxxxx
		'''
		config = configparser.ConfigParser()
		self.logger.info('reading credential config file: ' + str(credentialConfigPath))
		config.read_file(codecs.open(credentialConfigPath, 'r', 'utf8'))
		# TODO check validity of file before making these lookups, so we can tell the user their config file is bogus instead of just blowing up
		c_key = config['TwitterCredentials']['ConsumerKey']
		c_secret = config['TwitterCredentials']['ConsumerSecret']
		a_token = config['TwitterCredentials']['AccessToken']
		a_token_secret = config['TwitterCredentials']['AccessTokenSecret']
		
		self.connect(c_key, c_secret, a_token, a_token_secret)
		
	def tweet(self, message):
		'''
		Tweets the message provided (a string).
		
		Raises a ValueError if the message is over the character limit, None, or empty.
		If not connected to twitter yet, will attempt to load credentials out of the file credentials.ini in the configuration
		directory provided to the constrctor, and connect with those.
		'''
		tweet = None
		try:
			# error checking
			if self.api == None:
				self.connectFromConfig(self.getCredentialsConfigFilename())
			elif message == None:
				raise ValueError('Message cannot be None.')
			elif len(message) > EasyTweeter.CHARACTER_LIMIT:
				raise ValueError('message "' + str(message) + '" is over the ' + EasyTweeter.CHARACTER_LIMIT + ' character limit (it is ' + str(len(message)) + ' characters long).')
			elif len(message) <= 0:
				raise ValueError('Message cannot be empty.')
				
			retry = self.retries
			while retry > 0:
				try:
					# actual work
					self.logger.info('Tweeting message: ' + str(message))
					tweet = self.api.update_status(message)
					self.logger.info('Tweeted successfully')
					
					break
				except tweepy.RateLimitError as e:
					# handle rate limiting
					retry -= 1
					self.logger.warning('Twitter API rate limit reached.')
					self.logger.debug('Twitter API provides the following rate limit status information: ' + str(self.api.rate_limit_status()))
					if retry > 0:
						self.logger.info('Sleeping for ' + str(self.sleep) + ' seconds before retrying. Retries left: ' + str(retry))
						time.sleep(self.sleep)
					else:
						raise RateLimitRetriesExceeded('Retries have been exceeded.')
						
		except Exception as e:
			# log it and rethrow it, just so we can log exceptions without our users needing to do it
			self.logger.exception('Exception encountered while attempting to tweet')
			raise e
			
		if tweet != None:
			self.handleTweet(tweet) # TODO: error handling for hook methods?
	
	def checkForUpdates(self, runInterval = 7, retweets = True, newFollowers = True, replies = True, favorites = True, directMessages = False):
		'''
		Checks for updates to the twitter account that we've connected to, once every X times the method has been called,
		where X is the parameter runInterval, which defaults to 7 (once a week, if you run once a day).
		The number of times this method has been called since the last time a real check has occurred is saved to a file.
		
		If you would like to disable checking for retweets, new followers, replies, favorites or direct messages, pass false to one
		of the like-named parameters, and even if the run interval occurs, that check will not be made.
		It's typically the case that twitter applications are not given DM permissions to the accounts that they manage, so
		checking for direct messages is disabled by default.
		'''
		check = self._shouldCheckUpdates(runInterval)
		if runInterval <= 0 or check:
			if self.api == None:
				try:
					self.connectFromConfig(self.getCredentialsConfigFilename())
				except Exception as e:
					# log it and rethrow it, just so we can log exceptions without our users needing to do it
					self.logger.exception('Exception encountered while attempting to authenticate in order to check for updates')
					raise e
			
			# timer's up, let's check for updates.
			self.logger.info('Checking for updates on our twitter account...')
			
			if retweets:
				self.checkRetweets()
			else:
				self.logger.info('Skipping checking for new retweets.')
				
			if newFollowers:
				self.checkNewFollowers()
			else:
				self.logger.info('Skipping checking for new followers.')
				
			if favorites:
				self.checkFavorites()
			else:
				self.logger.info('Skipping checking for new favorites.')
				
			if directMessages:
				self.checkDirectMessages()
			else:
				self.logger.info('Skipping checking for new direct messages.')
				
			if replies:
				self.checkReplies()
			else:
				self.logger.info('Skipping checking for new replies.')
		
	###############################################################################################################
	# These are called by checkForUpdates, but you can call them yourself if you want to for some reason:
	def checkDirectMessages(self, maxMessagesLoaded = 50, failureOnMissingPermission = False):
		'''
		Checks for new direct messages to the authenticated user.
		Each new direct message is logged, and handleDM is called for each.
		
		It's typically the case that twitter applications are not given DM permissions to the accounts
		that they manage. If this is the case, attempting to get DMs returns an error 403.
		If the parameter failureOnMissingPermission is false (the default) and this error occurs,
		the error will be logged, and no exception will be raised.
		'''
		try:
			if self.api == None:
				self.connectFromConfig(self.getCredentialsConfigFilename())
			newMessages = []
			self.logger.info('Checking for new direct messages')
				
			# load the latest message to look for
			stateFilePath = os.path.join(self.getStateDirectory(), 'latest.ini')
			self._initState()
			if os.path.exists(stateFilePath):
				config = configparser.ConfigParser()
				self.logger.debug('reading latest messages file: ' + str(stateFilePath))
				config.read_file(codecs.open(stateFilePath, 'r', 'utf8'))
				if 'DirectMessages' in config:
					last = config['DirectMessages']['LatestSeen']
				else:
					self.logger.debug('Latest message file found, but no entry was found for direct messages.')
					last = None
			else:
				self.logger.debug('No latest messages file')
				last = None
				config = configparser.ConfigParser()
			
			# do we have any messages since then?
			try:
				if last == None:
					for dm in tweepy.Cursor(self.api.direct_messages, full_text = True).items(maxMessagesLoaded): # TODO: proper rate limiting handling
						newMessages.append(dm)
				else:
					for dm in tweepy.Cursor(self.api.direct_messages, full_text = True, since_id = last).items(maxMessagesLoaded): # TODO: proper rate limiting handling
						newMessages.append(dm)
			except tweepy.error.TweepError as e:
				if "status code = 403" in e.reason:
					# the users's twitter app lacks the DM permissions, and that's why we failed
					if failureOnMissingPermission:
						raise MissingPermissionsError("Your twitter user lacks the direct message permission.") from e
					else:
						self.logger.info("Twitter user lacks direct message permissions. This is the typical configuration for twitter apps. This error will be ignored, and direct messages will not be checked.")
				else:
					raise e # some other non-permission related error, so we'll keep it propogating
			newMessages.sort(key=lambda x: x.id, reverse=True)
			
			# save off the latest DM we got (if applicable)
			if not newMessages:
				self.logger.info('no new direct messages')
			else:
				last = newMessages[0].id
				self.logger.info(str(len(newMessages)) + " new direct messages")
				config['DirectMessages'] = {}
				config['DirectMessages']['LatestSeen'] = str(last)
				with open(stateFilePath, 'w') as stateFile:
					config.write(stateFile)
			
			for dm in newMessages:
				self.handleDM(dm) # TODO: error handling for hook methods?
			
			return newMessages
			
		except Exception as e:
			# log it and rethrow it, just so we can log exceptions without our users needing to do it
			self.logger.exception('Exception encountered while attempting to check direct messages')
			raise e
			
	def checkNewFollowers(self, maxFollowersChecked = 50):
		'''
		Checks for new followers to the twitter account.
		Each new follower is logged and handleFollower is called on each.
		'''
		try:
			if self.api == None:
				self.connectFromConfig(self.getCredentialsConfigFilename())
			newFollowers = []
			self.logger.info('Checking for new followers')
				
			# load up the list of known followers to compare new ones to
			knownFollowers = []
			stateFilePath = os.path.join(self.getStateDirectory(), 'followers.txt')
			self._initState()
			if os.path.exists(stateFilePath):
				self.logger.debug('opening followers file: ' + stateFilePath)
				with open(stateFilePath) as followersFile:
					for line in followersFile:
						knownFollowers.append(str(line).strip())
				self.logger.debug(str(len(knownFollowers)) + ' total known followers.')
			else:
				self.logger.debug('no followers file exists.')
			
			# look at all current followers
			for follower in tweepy.Cursor(self.api.followers).items(maxFollowersChecked): # TODO: proper rate limiting handling
				if not str(follower.id) in knownFollowers:
					# they're new!
					newFollowers.append(follower)
					knownFollowers.append(str(follower.id))
			
			if not newFollowers:
				self.logger.info('No new followers.')
			else:
				self.logger.info(str(len(newFollowers)) + ' new followers.')
				
				# save known followers list
				self.logger.debug('saving followers file with new additions')
				with open(stateFilePath, 'w') as followersFile:
					for id in knownFollowers:
						followersFile.write(id + "\n")
				self.logger.debug('followers file saved')
			
			for follower in newFollowers:
				self.handleNewFollower(follower) # TODO: error handling for hook methods?
			
			return newFollowers
			
		except Exception as e:
			# log it and rethrow it, just so we can log exceptions without our users needing to do it
			self.logger.exception('Exception encountered while attempting to check for new followers')
			raise e
		
	def checkRetweets(self, maxMessagesLoaded = 50):
		'''
		Checks for new retweets, and logs each retweet, and calls the method handleRetweet on this class for each.
		maxMessagesLoaded notes the max number of retweets that will be loaded.
		'''
		try:
			if self.api == None:
				self.connectFromConfig(self.getCredentialsConfigFilename())
			newMessages = []
			self.logger.info('Checking for new retweets')
				
			# load the latest message to look for
			stateFilePath = os.path.join(self.getStateDirectory(), 'latest.ini')
			self._initState()
			if os.path.exists(stateFilePath):
				config = configparser.ConfigParser()
				self.logger.debug('reading latest messages file: ' + str(stateFilePath))
				config.read_file(codecs.open(stateFilePath, 'r', 'utf8'))
				if 'Retweets' in config:
					last = config['Retweets']['LatestSeen']
				else:
					self.logger.debug('Latest message file found, but no entry was found for retweets.')
					last = None
			else:
				self.logger.debug('No latest messages file')
				last = None
				config = configparser.ConfigParser()
			
			# do we have any messages since then?
			if last == None:
				for retweet in tweepy.Cursor(self.api.retweets_of_me).items(maxMessagesLoaded): # TODO: proper rate limiting handling
					newMessages.append(retweet)
			else:
				for retweet in tweepy.Cursor(self.api.retweets_of_me, since_id = last).items(maxMessagesLoaded): # TODO: proper rate limiting handling
					newMessages.append(retweet)
			#newMessages.sort(key=lambda x: x.id, reverse=True)
			
			# save off the latest retweet we got (if applicable)
			if not newMessages:
				self.logger.info('no new retweets')
			else:
				last = newMessages[0].id
				self.logger.info(str(len(newMessages)) + " new retweets")
				config['Retweets'] = {}
				config['Retweets']['LatestSeen'] = str(last)
				with open(stateFilePath, 'w') as stateFile:
					config.write(stateFile)
			
			for retweet in newMessages:
				self.handleRetweet(retweet) # TODO: error handling for hook methods?
			
			return newMessages
			
		except Exception as e:
			# log it and rethrow it, just so we can log exceptions without our users needing to do it
			self.logger.exception('Exception encountered while attempting to check for retweets')
			raise e
		
	def checkReplies(self, maxMessagesLoaded = 100):
		'''
		Checks for replies to posts made by the authenticated user.
		Each new reply is logged and the method handleReply is called on each.
		Up to maxMessagesLoaded messages are loaded.
		
		Unfortunately, twitter's api makes checking for this extraordinarily difficult: the only viable way to do it is check our
		own home timeline (the same thing you get if you just go to twitter.com) and look to see if there is anything on it which
		is a reply to a message we posted. I got this from twitter's documentation. There's no other 'real' or good way to do it.
		There is no such thing as a query for replies.
		I would call this a hack, but it's officially endorsed by twitter, which in my opinion makes it even worse, but regardless,
		it's what we've got. This means that if you want this bot to alert you of replies reliably, you either must run it pretty
		frequently, such that the maximum total number of relevant results isn't enough to rate-limit you, or your bot account
		cannot follow alot of people, keeping the length of the timeline low enough that you don't rate-limit yourself by getting
		the whole thing.
		'''
		try:
			if self.api == None:
				self.connectFromConfig(self.getCredentialsConfigFilename())
			newMessages = []
			self.logger.info('Checking for new replies')
				
			# load the latest message to look for
			stateFilePath = os.path.join(self.getStateDirectory(), 'latest.ini')
			self._initState()
			if os.path.exists(stateFilePath):
				config = configparser.ConfigParser()
				self.logger.debug('reading latest messages file: ' + str(stateFilePath))
				config.read_file(codecs.open(stateFilePath, 'r', 'utf8'))
				if 'Replies' in config:
					last = config['Replies']['LatestSeen']
				else:
					self.logger.debug('Latest message file found, but no entry was found for replies.')
					last = None
			else:
				self.logger.debug('No latest messages file')
				last = None
				config = configparser.ConfigParser()
			
			# do we have any messages since then?
			if last == None:
				for msg in tweepy.Cursor(self.api.home_timeline).items(maxMessagesLoaded): # TODO: proper rate limiting handling
					if msg.in_reply_to_user_id == self.api.me().id:
						newMessages.append(msg)
			else:
				for msg in tweepy.Cursor(self.api.home_timeline, since_id = last).items(maxMessagesLoaded): # TODO: proper rate limiting handling
					if msg.in_reply_to_user_id == self.api.me().id:
						newMessages.append(msg)
			newMessages.sort(key=lambda x: x.id, reverse=True)
			
			# save off the latest reply we got (if applicable)
			if not newMessages:
				self.logger.info('no new replies')
			else:
				last = newMessages[0].id
				self.logger.info(str(len(newMessages)) + " new replies")
				config['Replies'] = {}
				config['Replies']['LatestSeen'] = str(last)
				with open(stateFilePath, 'w') as stateFile:
					config.write(stateFile)
			
			for reply in newMessages:
				self.handleReply(reply) # TODO: error handling for hook methods?
			
			return newMessages
			
		except Exception as e:
			# log it and rethrow it, just so we can log exceptions without our users needing to do it
			self.logger.exception('Exception encountered while attempting to check for replies')
			raise e
		
	def checkFavorites(self, maxMessagesLoaded = 50):
		'''
		Checks for new favorites on statuses posted by this user.
		Each favorite is logged, and handleFavorite is called on each.
		
		The way this is accomplished is via loading up a list of the latest few tweets we've made (up to maxMessagesLoaded),
		and checking each of their favorite totals.
		'''
		try:
			if self.api == None:
				self.connectFromConfig(self.getCredentialsConfigFilename())
			newFavorites = []
			self.logger.info('Checking for new favorites')
				
			# load the favorite file (this is how we know if we've seen a favorite before or not)
			stateFilePath = os.path.join(self.getStateDirectory(), 'favorites.ini')
			self._initState()
			if os.path.exists(stateFilePath):
				config = configparser.ConfigParser()
				self.logger.debug('reading favorite messages file: ' + str(stateFilePath))
				config.read_file(codecs.open(stateFilePath, 'r', 'utf8'))
			else:
				self.logger.debug('No favorite messages file')
				config = configparser.ConfigParser()
			
			# look at our latest N tweets to check for how many favorites they have
			for msg in tweepy.Cursor(self.api.user_timeline).items(maxMessagesLoaded): # TODO: proper rate limiting handling
				if msg.favorite_count > 0:
					if str(msg.id) in config:
						known = config[str(msg.id)]['favorites']
					else:
						known = 0
					if str(msg.favorite_count) != known:
						config[str(msg.id)] = {}
						config[str(msg.id)]['favorites'] = str(msg.favorite_count)
						newFavorites.append((msg, known))
					else:
						self.logger.debug('message id ' + str(msg.id) + " has " + str(msg.favorite_count) + " favorites, but that value is already known.")
			
			# save off the state config
			if not newFavorites:
				self.logger.info('no new favorites')
			else:
				self.logger.info(str(len(newFavorites)) + " new favorites")
				with open(stateFilePath, 'w') as stateFile:
					config.write(stateFile)
			
			for newFavorite in newFavorites:
				self.handleFavorite(newFavorite[0], newFavorite[1]) # TODO: error handling for hook methods?
			
			return newFavorites
			
		except Exception as e:
			# log it and rethrow it, just so we can log exceptions without our users needing to do it
			self.logger.exception('Exception encountered while attempting to check for favorites')
			raise e
		


###########################
# Here's how you use this class:
# Put your api credentials in the file example_bot/credentials.ini, and run the below code.
# Hopefully you'll agree that the "easy" in the class's name isn't false advertising.
###########################
if __name__ == "__main__":
	bot = EasyTweeter('example_bot')
	bot.tweet('test tweet please ignore') # <--- insert some fancy text generation stuff here
	bot.checkForUpdates() # If all you want to do is tweet, and don't care to log or process replies, favorites, retweets, new followers, or direct messages, this line is optional
	
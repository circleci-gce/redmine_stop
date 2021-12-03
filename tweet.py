# coding:utf-8

import os
import sys
import tweepy

args = sys.argv

# Create twitter objects
auth = tweepy.OAuthHandler(args[1], args[2])
auth.set_access_token(args[3], args[4])
api = tweepy.API(auth)

# Create message body
URL = os.environ['CIRCLE_BUILD_URL']
message = '【CircleCI stopped VM instance】' + '\n\n' \
          + 'Instance Name: Redmica' + '\n\n' \
          + 'Python version' + '\n' \
          + sys.version + '\n\n' \
          + 'See details' + '\n' \
          + '↓↓↓↓↓↓' + '\n' \
          + URL

# Send direct message
api.send_direct_message(args[5], message)

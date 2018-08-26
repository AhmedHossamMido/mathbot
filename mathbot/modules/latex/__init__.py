import random
import os
import safe
import PIL
import PIL.Image
import PIL.ImageDraw
import PIL.ImageFont
import io
import core.settings
import urllib
import aiohttp
import asyncio
import traceback
import re
import imageutil
import core.help
import advertising
import discord
import json
from open_relative import *
from discord.ext.commands import command
from utils import is_private, MessageEditGuard

core.help.load_from_file('./help/latex.md')


LATEX_SERVER_URL = 'http://rtex.probablyaweb.site/api/v2'


# Load data from external files

def load_templates():
	with open_relative('template.tex', encoding = 'utf-8') as f:
		raw = f.read()
	# Remove any comments from the template
	cleaned = re.sub(r'%.*\n', '', raw)
	template = cleaned.replace('#BLOCK', 'gather*')
	t_inline = cleaned.replace('#BLOCK', 'flushleft')
	return template, t_inline

TEMPLATE, TEMPLATE_INLINE = load_templates()

with open_relative('replacements.json', encoding = 'utf-8') as _f:
	repl_json = _f.read()
TEX_REPLACEMENTS = json.loads(repl_json)


# Error messages

LATEX_TIMEOUT_MESSAGE = 'The renderer took too long to respond.'

PERMS_FAILURE = '''\
I don't have permission to upload images here :frowning:
The owner of this server should be able to fix this issue.
'''

DELETE_PERMS_FAILURE = '''\
The bot has been set up to delete `=tex` command inputs.
It requires the **manage messages** permission in order to do this.
'''


class RenderingError(Exception):
	pass


class LatexModule:

	def __init__(self, bot):
		self.bot = bot
		# Keep track of recently fulfilled requests
		# Holds dictionaries of
		# {
		# 	'template': (either 'normal' or 'inline', depending on what was used),
		# 	'message': the ID of the _output_ message
		# }
		self.connections = {}

	@command(aliases=['latex', 'rtex'])
	async def tex(self, context, *, latex=''):
		if latex == '':
			await context.send('Type `=help tex` for information on how to use this command.')
		elif False or not has_required_perms(context.channel): # TODO: Fix this
			await context.send(PERMS_FAILURE)
		else:
			await self.handle(context.message, latex, 'normal')
			# TODO: Reenable this
			# if await context.bot.settings.get(message, 'f-delete-tex'):
			# 	await asyncio.sleep(10)
			# 	try:
			# 		await context.message.delete()
			# 	except discord.errors.NotFound:
			# 		pass
			# 	except discord.errors.Forbidden:
			# 		await context.send(PERMS_FAILURE)

	# TODO: Get edits working again
	# @command_latex.edit(require_before = False, require_after = True)
	# async def handle_edit(self, before, after, latex):
	# 	if latex != '' and before.content != after.content:
	# 		blob = self.connections.get(before.id, {'template': 'normal'})
	# 		try:
	# 			await self.client.delete_message(blob['message'])
	# 		except Exception:
	# 			pass
	# 		# print('Handling edit:', latex)
	# 		await self.handle(after, latex, blob.get('template'))

	async def on_message(self, message):
		# TODO: Filter out messages that start with the server prefix.
		if message.content.count('$$') >= 2:
			if is_private(message.channel) or (await self.bot.settings.get_setting(message, 'c-tex') and await self.bot.settings.get_setting(message, 'f-inline-tex')):
				latex = extract_inline_tex(message.clean_content)
				if latex != '':
					await self.handle(message, latex, 'inline')

	# async def inline_edit(self, before, after):
	# 	server_prefix = await core.settings.get_server_prefix(before)
	# 	if not after.content.startswith(server_prefix) and after.content.count('$$') >= 2 and before.content != after.content:
	# 		if after.channel.is_private or (await core.settings.get_setting(after, 'c-tex') and await core.settings.get_setting(after, 'f-inline-tex')):
	# 			blob = self.connections.get(before.id, {'template': 'inline'})
	# 			try:
	# 				await self.client.delete_message(blob['message'])
	# 			except Exception:
	# 				pass
	# 			latex = extract_inline_tex(after.clean_content)
	# 			if latex != '':
	# 				await self.handle(after, latex, blob.get('template'))

	async def handle(self, message, latex, template):
		print(f'LaTeX - {message.author} - {latex}')
		colour_back, colour_text = await self.get_colours(message.author)
		tpl = TEMPLATE if template == 'normal' else TEMPLATE_INLINE
		latex = tpl.replace('#COLOUR',  colour_text) \
		           .replace('#CONTENT', process_latex(latex))
		sent_message = await self.render_and_reply(message, latex, colour_back)
		if sent_message != None:
			await core.blame.set_blame(self.bot.keystore, sent_message, message.author)
			self.connections[message.id] = {
				'message': sent_message,
				'template': template
			}

	async def render_and_reply(self, message, latex, colour_back):
		async with message.channel.typing():
			with MessageEditGuard(message) as guard:
				try:
					render_result = await generate_image_online(latex, colour_back)
				except asyncio.TimeoutError:
					return await guard.send(LATEX_TIMEOUT_MESSAGE)
				except RenderingError:
					return await guard.send('Rendering failed. Check your code. You can edit your existing message if needed.')
				else:
					content = None
					# TODO: Enable this again
					# if await advertising.should_advertise_to(context.message.author, context.channel):
					# 	content = 'Support the bot on Patreon: <https://www.patreon.com/dxsmiley>'
					return await guard.send(content, file=discord.File(render_result, 'latex.png'))

	async def get_colours(self, user):
		colour_setting = await self.bot.keystore.get('p-tex-colour', str(user.id)) or 'dark'
		if colour_setting == 'light':
			return 'ffffff', '202020'
		elif colour_setting == 'dark':
			return '36393E', 'f0f0f0'
		# Fallback in case of other weird things
		return '36393E', 'f0f0f0'
		# raise ValueError('{} is not a valid colour scheme'.format(colour_setting))


async def generate_image_online(latex, colour_back):
	payload = {
		'format': 'png',
		'code': latex.strip(),
	}
	async with aiohttp.ClientSession() as session:
		try:
			async with session.post(LATEX_SERVER_URL, json=payload, timeout=8) as loc_req:
				loc_req.raise_for_status()
				jdata = await loc_req.json()
				if jdata['status'] == 'error':
					raise RenderingError
				filename = jdata['filename']
			# Now actually get the image
			async with session.get(LATEX_SERVER_URL + '/' + filename, timeout=3) as img_req:
				img_req.raise_for_status()
				fo = io.BytesIO(await img_req.read())
				image = PIL.Image.open(fo).convert('RGBA')
		except aiohttp.client_exceptions.ClientResponseError:
			raise RenderingError
	border_size = 4
	colour_back = imageutil.hex_to_tuple(colour_back)
	width, height = image.size
	backing = imageutil.new_monocolour((width + border_size * 2, height + border_size * 2), colour_back)
	backing.paste(image, (border_size, border_size), image)
	fobj = io.BytesIO()
	backing.save(fobj, format='PNG')
	fobj = io.BytesIO(fobj.getvalue())
	return fobj


def extract_inline_tex(content):
	parts = iter(content.split('$$'))
	latex = ''
	try:
		while True:
			word = next(parts)
			if word != '':
				latex += word.replace('#', '\\#') \
						     .replace('$', '\\$') \
						     .replace('%', '\\%')
				latex += ' '
			word = next(parts)
			if word != '':
				latex += '$\\displaystyle {}$ '.format(word.strip('`'))
	except StopIteration:
		pass
	return latex.rstrip()


def process_latex(latex):
	latex = latex.replace('`', ' ').strip(' \n')
	if latex.startswith('tex'):
		latex = latex[3:]
	for key, value in TEX_REPLACEMENTS.items():
		if key in latex:
			latex = latex.replace(key, value)
	return latex


def has_required_perms(channel):
	if is_private(channel):
		return True
	perms = channel.permissions_for(channel.server.me)
	return perms.attach_files


def setup(bot):
	bot.add_cog(LatexModule(bot))

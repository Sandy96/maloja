# server
from bottle import request, response, FormsDict
# rest of the project
from cleanup import *
from utilities import *
from malojatime import *
from urihandler import uri_to_internal, internal_to_uri, compose_querystring
import compliant_api
# doreah toolkit
from doreah.logging import log
from doreah import tsv
from doreah.caching import Cache, DeepCache
try:
	from doreah.persistence import DiskDict
except: pass
import doreah
# technical
import os
import datetime
import sys
import unicodedata
from collections import namedtuple
from threading import Lock
# url handling
from importlib.machinery import SourceFileLoader
import urllib




dblock = Lock() #global database lock

SCROBBLES = []	# Format: tuple(track_ref,timestamp,saved)
ARTISTS = []	# Format: artist
TRACKS = []	# Format: namedtuple(artists=frozenset(artist_ref,...),title=title)


Track = namedtuple("Track",["artists","title"])
Scrobble = namedtuple("Scrobble",["track","timestamp","saved"])

### OPTIMIZATION
SCROBBLESDICT = {}	# timestamps to scrobble mapping
STAMPS = []		# sorted
#STAMPS_SET = set()	# as set for easier check if exists # we use the scrobbles dict for that now
TRACKS_LOWER = []
ARTISTS_LOWER = []
ARTIST_SET = set()
TRACK_SET = set()
MEDALS = {}	#literally only changes once per year, no need to calculate that on the fly
MEDALS_TRACKS = {}

cla = CleanerAgent()
coa = CollectorAgent()
clients = []

lastsync = 0

# rulestate that the entire current database was built with, or False if the database was built from inconsistent scrobble files
db_rulestate = False



### symmetric keys are fine for now since we hopefully use HTTPS
def loadAPIkeys():
	global clients
	tsv.create("clients/authenticated_machines.tsv")
	#createTSV("clients/authenticated_machines.tsv")
	clients = tsv.parse("clients/authenticated_machines.tsv","string","string")
	#clients = parseTSV("clients/authenticated_machines.tsv","string","string")
	log("Authenticated Machines: " + ", ".join([m[1] for m in clients]))

def checkAPIkey(k):
	return (k in [k for [k,d] in clients])
def allAPIkeys():
	return [k for [k,d] in clients]


####
## Getting dict representations of database objects
####

def get_scrobble_dict(o):
	track = get_track_dict(TRACKS[o.track])
	return {"artists":track["artists"],"title":track["title"],"time":o.timestamp}

def get_artist_dict(o):
	return o
	#technically not a dict, but... you know

def get_track_dict(o):
	artists = [get_artist_dict(ARTISTS[a]) for a in o.artists]
	return {"artists":artists,"title":o.title}


####
## Creating or finding existing database entries
####



def createScrobble(artists,title,time,volatile=False):
	dblock.acquire()
	while (time in SCROBBLESDICT):
		time += 1
	i = getTrackID(artists,title)
	obj = Scrobble(i,time,volatile) # if volatile generated, we simply pretend we have already saved it to disk
	#SCROBBLES.append(obj)
	# immediately insert scrobble correctly so we can guarantee sorted list
	index = insert(SCROBBLES,obj,key=lambda x:x[1])
	SCROBBLESDICT[time] = obj
	STAMPS.insert(index,time) #should be same index as scrobblelist
	register_scrobbletime(time)
	invalidate_caches()
	dblock.release()

	return get_track_dict(TRACKS[obj.track])


# this will never be called from different threads, so no lock
def readScrobble(artists,title,time):
	while (time in SCROBBLESDICT):
		time += 1
	i = getTrackID(artists,title)
	obj = Scrobble(i,time,True)
	SCROBBLES.append(obj)
	SCROBBLESDICT[time] = obj
	#STAMPS.append(time)



def getArtistID(name):

	obj = name
	objlower = name.lower().replace("'","")

	if objlower in ARTIST_SET:
		return ARTISTS_LOWER.index(objlower)

	else:
		i = len(ARTISTS)
		ARTISTS.append(obj)
		ARTIST_SET.add(objlower)
		ARTISTS_LOWER.append(objlower)

		# with a new artist added, we might also get new artists that they are credited as
		cr = coa.getCredited(name)
		getArtistID(cr)

		coa.updateIDs(ARTISTS)

		return i

def getTrackID(artists,title):
	artistset = set()
	for a in artists:
		artistset.add(getArtistID(name=a))
	obj = Track(artists=frozenset(artistset),title=title)
	objlower = Track(artists=frozenset(artistset),title=title.lower().replace("'",""))

	if objlower in TRACK_SET:
		return TRACKS_LOWER.index(objlower)
	else:
		i = len(TRACKS)
		TRACKS.append(obj)
		TRACK_SET.add(objlower)
		TRACKS_LOWER.append(objlower)
		return i










########
########
## HTTP requests and their associated functions
########
########


# silly patch to get old syntax working without dbserver

# function to register all the functions to the real server
def register_subroutes(server,path):
	for subpath in dbserver.handlers_get:
		func = dbserver.handlers_get[subpath]
		decorator = server.get(path + subpath)
		decorator(func)
	for subpath in dbserver.handlers_post:
		func = dbserver.handlers_post[subpath]
		decorator = server.post(path + subpath)
		decorator(func)


# fake server
class FakeBottle:
	def __init__(self):
		self.handlers_get = {}
		self.handlers_post = {}

	# these functions pretend that they're the bottle decorators, but only write
	# down which functions asked for them so they can later report their names
	# to the real bottle server
	def get(self,path):
		def register(func):
			self.handlers_get[path] = func
			return func
		return register
	def post(self,path):
		def register(func):
			self.handlers_post[path] = func
			return func
		return register

	def route(self,path):
		return self.get(path)


dbserver = FakeBottle()




@dbserver.route("/test")
def test_server():
	apikey = request.query.get("key")
	response.set_header("Access-Control-Allow-Origin","*")
	if apikey is not None and not (checkAPIkey(apikey)):
		response.status = 403
		return "Wrong API key"

	elif db_rulestate:
		response.status = 204
		return
	else:
		response.status = 205
		return

	# 204	Database server is up and operational
	# 205	Database server is up, but DB is not fully built or is inconsistent
	# 403	Database server is up, but provided API key is not valid


## All database functions are separated - the external wrapper only reads the request keys, converts them into lists and renames them where necessary, and puts the end result in a dict if not already so it can be returned as json

@dbserver.route("/scrobbles")
def get_scrobbles_external():
	keys = FormsDict.decode(request.query)
	k_filter, k_time, _, k_amount = uri_to_internal(keys)
	ckeys = {**k_filter, **k_time, **k_amount}

	result = get_scrobbles(**ckeys)
	return {"list":result}

def get_scrobbles(**keys):
	r = db_query(**{k:keys[k] for k in keys if k in ["artist","artists","title","since","to","within","timerange","associated","track","max_"]})
	#if keys.get("max_") is not None:
	#	return r[:int(keys.get("max_"))]
	#else:
	#	return r
	return r





# UNUSED
#@dbserver.route("/amounts")
#def get_amounts_external():
#	return get_amounts() #really now
#
#def get_amounts():
#	return {"scrobbles":len(SCROBBLES),"tracks":len(TRACKS),"artists":len(ARTISTS)}


@dbserver.route("/numscrobbles")
def get_scrobbles_num_external():
	keys = FormsDict.decode(request.query)
	k_filter, k_time, _, k_amount = uri_to_internal(keys)
	ckeys = {**k_filter, **k_time, **k_amount}

	result = get_scrobbles_num(**ckeys)
	return {"amount":result}

def get_scrobbles_num(**keys):
	r = db_query(**{k:keys[k] for k in keys if k in ["artist","track","artists","title","since","to","within","timerange","associated"]})
	return len(r)


#for multiple since values (must be ordered)
# DOESN'T SEEM TO ACTUALLY BE FASTER
# REEVALUATE

#def get_scrobbles_num_multiple(sinces=[],to=None,**keys):
#
#	sinces_stamps = [time_stamps(since,to,None)[0] for since in sinces]
#	#print(sinces)
#	#print(sinces_stamps)
#	minsince = sinces[-1]
#	r = db_query(**{k:keys[k] for k in keys if k in ["artist","track","artists","title","associated","to"]},since=minsince)
#
#	#print(r)
#
#	validtracks = [0 for s in sinces]
#
#	i = 0
#	si = 0
#	while True:
#		if si == len(sinces): break
#		if i == len(r): break
#		if r[i]["time"] >= sinces_stamps[si]:
#			validtracks[si] += 1
#		else:
#			si += 1
#			continue
#		i += 1
#
#
#	return validtracks


# UNUSED
#@dbserver.route("/charts")
#def get_charts_external():
#	keys = FormsDict.decode(request.query)
#	ckeys = {}
#	ckeys["since"], ckeys["to"], ckeys["within"] = keys.get("since"), keys.get("to"), keys.get("in")
#
#	result = get_scrobbles_num(**ckeys)
#	return {"number":result}

#def get_charts(**keys):
#	return db_aggregate(**{k:keys[k] for k in keys if k in ["since","to","within"]})







@dbserver.route("/tracks")
def get_tracks_external():
	keys = FormsDict.decode(request.query)
	k_filter, _, _, _ = uri_to_internal(keys,forceArtist=True)
	ckeys = {**k_filter}

	result = get_tracks(**ckeys)
	return {"list":result}

def get_tracks(artist=None):

	if artist is not None:
		artistid = ARTISTS.index(artist)
	else:
		artistid = None

	# Option 1
	return [get_track_dict(t) for t in TRACKS if (artistid in t.artists) or (artistid==None)]

	# Option 2 is a bit more elegant but much slower
	#tracklist = [get_track_dict(t) for t in TRACKS]
	#ls = [t for t in tracklist if (artist in t["artists"]) or (artist==None)]


@dbserver.route("/artists")
def get_artists_external():
	result = get_artists()
	return {"list":result}

def get_artists():
	return ARTISTS #well







@dbserver.route("/charts/artists")
def get_charts_artists_external():
	keys = FormsDict.decode(request.query)
	_, k_time, _, _ = uri_to_internal(keys)
	ckeys = {**k_time}

	result = get_charts_artists(**ckeys)
	return {"list":result}

def get_charts_artists(**keys):
	return db_aggregate(by="ARTIST",**{k:keys[k] for k in keys if k in ["since","to","within","timerange"]})






@dbserver.route("/charts/tracks")
def get_charts_tracks_external():
	keys = FormsDict.decode(request.query)
	k_filter, k_time, _, _ = uri_to_internal(keys,forceArtist=True)
	ckeys = {**k_filter, **k_time}

	result = get_charts_tracks(**ckeys)
	return {"list":result}

def get_charts_tracks(**keys):
	return db_aggregate(by="TRACK",**{k:keys[k] for k in keys if k in ["since","to","within","timerange","artist"]})









@dbserver.route("/pulse")
def get_pulse_external():
	keys = FormsDict.decode(request.query)
	k_filter, k_time, k_internal, k_amount = uri_to_internal(keys)
	ckeys = {**k_filter, **k_time, **k_internal, **k_amount}

	results = get_pulse(**ckeys)
	return {"list":results}

def get_pulse(**keys):

	rngs = ranges(**{k:keys[k] for k in keys if k in ["since","to","within","timerange","step","stepn","trail"]})
	results = []
	for rng in rngs:
		res = len(db_query(timerange=rng,**{k:keys[k] for k in keys if k in ["artists","artist","track","title","associated"]}))
		results.append({"range":rng,"scrobbles":res})

	return results





@dbserver.route("/performance")
def get_performance_external():
	keys = FormsDict.decode(request.query)
	k_filter, k_time, k_internal, k_amount = uri_to_internal(keys)
	ckeys = {**k_filter, **k_time, **k_internal, **k_amount}

	results = get_performance(**ckeys)
	return {"list":results}

def get_performance(**keys):

	rngs = ranges(**{k:keys[k] for k in keys if k in ["since","to","within","timerange","step","stepn","trail"]})
	results = []

	for rng in rngs:
		if "track" in keys:
			charts = get_charts_tracks(timerange=rng)
			rank = None
			for c in charts:
				if c["track"] == keys["track"]:
					rank = c["rank"]
					break
		elif "artist" in keys:
			charts = get_charts_artists(timerange=rng)
			rank = None
			for c in charts:
				if c["artist"] == keys["artist"]:
					rank = c["rank"]
					break
		results.append({"range":rng,"rank":rank})

	return results








@dbserver.route("/top/artists")
def get_top_artists_external():

	keys = FormsDict.decode(request.query)
	_, k_time, k_internal, _ = uri_to_internal(keys)
	ckeys = {**k_time, **k_internal}

	results = get_top_artists(**ckeys)
	return {"list":results}

def get_top_artists(**keys):

	rngs = ranges(**{k:keys[k] for k in keys if k in ["since","to","within","timerange","step","stepn","trail"]})
	results = []

	for rng in rngs:
		try:
			res = db_aggregate(timerange=rng,by="ARTIST")[0]
			results.append({"range":rng,"artist":res["artist"],"counting":res["counting"],"scrobbles":res["scrobbles"]})
		except:
			results.append({"range":rng,"artist":None,"scrobbles":0})

	return results










@dbserver.route("/top/tracks")
def get_top_tracks_external():
	keys = FormsDict.decode(request.query)
	_, k_time, k_internal, _ = uri_to_internal(keys)
	ckeys = {**k_time, **k_internal}

	# IMPLEMENT THIS FOR TOP TRACKS OF ARTIST AS WELL?

	results = get_top_tracks(**ckeys)
	return {"list":results}

def get_top_tracks(**keys):

	rngs = ranges(**{k:keys[k] for k in keys if k in ["since","to","within","timerange","step","stepn","trail"]})
	results = []

	for rng in rngs:
		try:
			res = db_aggregate(timerange=rng,by="TRACK")[0]
			results.append({"range":rng,"track":res["track"],"scrobbles":res["scrobbles"]})
		except:
			results.append({"range":rng,"track":None,"scrobbles":0})

	return results











@dbserver.route("/artistinfo")
def artistInfo_external():
	keys = FormsDict.decode(request.query)
	k_filter, _, _, _ = uri_to_internal(keys,forceArtist=True)
	ckeys = {**k_filter}

	results = artistInfo(**ckeys)
	return results

def artistInfo(artist):

	charts = db_aggregate(by="ARTIST")
	scrobbles = len(db_query(artists=[artist])) #we cant take the scrobble number from the charts because that includes all countas scrobbles
	try:
		c = [e for e in charts if e["artist"] == artist][0]
		others = [a for a in coa.getAllAssociated(artist) if a in ARTISTS]
		position = c["rank"]
		return {"scrobbles":scrobbles,"position":position,"associated":others,"medals":MEDALS.get(artist)}
	except:
		# if the artist isnt in the charts, they are not being credited and we need to show information about the credited one
		artist = coa.getCredited(artist)
		c = [e for e in charts if e["artist"] == artist][0]
		position = c["rank"]
		return {"replace":artist,"scrobbles":scrobbles,"position":position}





@dbserver.route("/trackinfo")
def trackInfo_external():
	keys = FormsDict.decode(request.query)
	k_filter, _, _, _ = uri_to_internal(keys,forceTrack=True)
	ckeys = {**k_filter}

	results = trackInfo(**ckeys)
	return results

def trackInfo(artists,title):
	charts = db_aggregate(by="TRACK")
	#scrobbles = len(db_query(artists=artists,title=title))	#chart entry of track always has right scrobble number, no countas rules here
	c = [e for e in charts if set(e["track"]["artists"]) == set(artists) and e["track"]["title"] == title][0]
	scrobbles = c["scrobbles"]
	position = c["rank"]

	return {"scrobbles":scrobbles,"position":position,"medals":MEDALS_TRACKS.get((frozenset(artists),title))}







@dbserver.get("/newscrobble")
def pseudo_post_scrobble():
	keys = FormsDict.decode(request.query) # The Dal★Shabet handler
	artists = keys.get("artist")
	title = keys.get("title")
	apikey = keys.get("key")
	if not (checkAPIkey(apikey)):
		response.status = 403
		return ""
	try:
		time = int(keys.get("time"))
	except:
		time = int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp())
	(artists,title) = cla.fullclean(artists,title)

	## this is necessary for localhost testing
	response.set_header("Access-Control-Allow-Origin","*")

	trackdict = createScrobble(artists,title,time)

	if (time - lastsync) > 3600:
		sync()

	return {"status":"success","track":trackdict}

@dbserver.post("/newscrobble")
def post_scrobble():
	keys = FormsDict.decode(request.forms) # The Dal★Shabet handler
	artists = keys.get("artist")
	title = keys.get("title")
	apikey = keys.get("key")
	if not (checkAPIkey(apikey)):
		response.status = 403
		return ""

	try:
		time = int(keys.get("time"))
	except:
		time = int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp())
	(artists,title) = cla.fullclean(artists,title)

	## this is necessary for localhost testing
	#response.set_header("Access-Control-Allow-Origin","*")

	trackdict = createScrobble(artists,title,time)

	#if (time - lastsync) > 3600:
	#	sync()
	sync()
	#always sync, one filesystem access every three minutes shouldn't matter


	return {"status":"success","track":trackdict}



# standard-compliant scrobbling methods

@dbserver.post("/s/<path:path>")
@dbserver.get("/s/<path:path>")
def sapi(path):
	path = path.split("/")
	path = list(filter(None,path))
	headers = request.headers
	if request.get_header("Content-Type") is not None and "application/json" in request.get_header("Content-Type"):
		keys = request.json
	else:
		keys = FormsDict.decode(request.params)
	auth = request.auth
	return compliant_api.handle(path,keys,headers,auth)




@dbserver.route("/sync")
def abouttoshutdown():
	sync()
	#sys.exit()

@dbserver.post("/newrule")
def newrule():
	keys = FormsDict.decode(request.forms)
	apikey = keys.pop("key",None)
	if (checkAPIkey(apikey)):
		tsv.add_entry("rules/webmade.tsv",[k for k in keys])
		#addEntry("rules/webmade.tsv",[k for k in keys])
		global db_rulestate
		db_rulestate = False


@dbserver.route("/issues")
def issues_external(): #probably not even needed
	return issues()


def issues():
	combined = []
	duplicates = []
	newartists = []
	inconsistent = not db_rulestate
	# if the user manually edits files while the server is running this won't show, but too lazy to check the rulestate here

	import itertools
	import difflib

	sortedartists = ARTISTS.copy()
	sortedartists.sort(key=len,reverse=True)
	reversesortedartists = sortedartists.copy()
	reversesortedartists.reverse()
	for a in reversesortedartists:

		nochange = cla.confirmedReal(a)

		st = a
		lis = []
		reachedmyself = False
		for ar in sortedartists:
			if (ar != a) and not reachedmyself:
				continue
			elif not reachedmyself:
				reachedmyself = True
				continue

			if (ar.lower() == a.lower()) or ("the " + ar.lower() == a.lower()) or ("a " + ar.lower() == a.lower()):
				duplicates.append((ar,a))
				break

			if (ar + " " in st) or (" " + ar in st):
				lis.append(ar)
				st = st.replace(ar,"").strip()
			elif (ar == st):
				lis.append(ar)
				st = ""
				if not nochange:
					combined.append((a,lis))
				break

			elif (ar in st) and len(ar)*2 > len(st):
				duplicates.append((a,ar))

		st = st.replace("&","").replace("and","").replace("with","").strip()
		if st != "" and st != a:
			if len(st) < 5 and len(lis) == 1:
				#check if we havent just randomly found the string in another word
				#if (" " + st + " ") in lis[0] or (lis[0].endswith(" " + st)) or (lis[0].startswith(st + " ")):
				duplicates.append((a,lis[0]))
			elif len(st) < 5 and len(lis) > 1 and not nochange:
				combined.append((a,lis))
			elif len(st) >= 5 and not nochange:
				#check if we havent just randomly found the string in another word
				if (" " + st + " ") in a or (a.endswith(" " + st)) or (a.startswith(st + " ")):
					newartists.append((st,a,lis))

	#for c in itertools.combinations(ARTISTS,3):
	#	l = list(c)
	#	print(l)
	#	l.sort(key=len,reverse=True)
	#	[full,a1,a2] = l
	#	if (a1 + " " + a2 in full) or (a2 + " " + a1 in full):
	#		combined.append((full,a1,a2))


	#for c in itertools.combinations(ARTISTS,2):
	#	if
	#
	#	if (c[0].lower == c[1].lower):
	#		duplicates.append((c[0],c[1]))


	#	elif (c[0] + " " in c[1]) or (" " + c[0] in c[1]) or (c[1] + " " in c[0]) or (" " + c[1] in c[0]):
	#		if (c[0] in c[1]):
	#			full, part = c[1],c[0]
	#			rest = c[1].replace(c[0],"").strip()
	#		else:
	#			full, part = c[0],c[1]
	#			rest = c[0].replace(c[1],"").strip()
	#		if rest in ARTISTS and full not in [c[0] for c in combined]:
	#			combined.append((full,part,rest))

	#	elif (c[0] in c[1]) or (c[1] in c[0]):
	#		duplicates.append((c[0],c[1]))


	return {"duplicates":duplicates,"combined":combined,"newartists":newartists,"inconsistent":inconsistent}


@dbserver.post("/importrules")
def import_rulemodule():
	keys = FormsDict.decode(request.forms)
	apikey = keys.pop("key",None)

	if (checkAPIkey(apikey)):
		filename = keys.get("filename")
		remove = keys.get("remove") is not None
		validchars = "-_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
		filename = "".join(c for c in filename if c in validchars)

		if remove:
			log("Deactivating predefined rulefile " + filename)
			os.remove("rules/" + filename + ".tsv")
		else:
			log("Importing predefined rulefile " + filename)
			os.symlink("predefined/" + filename + ".tsv","rules/" + filename + ".tsv")



@dbserver.post("/rebuild")
def rebuild():

	keys = FormsDict.decode(request.forms)
	apikey = keys.pop("key",None)
	if (checkAPIkey(apikey)):
		log("Database rebuild initiated!")
		global db_rulestate
		db_rulestate = False
		sync()
		os.system("python3 fixexisting.py")
		global cla, coa
		cla = CleanerAgent()
		coa = CollectorAgent()
		build_db()
		invalidate_caches()




@dbserver.get("/search")
def search():
	keys = FormsDict.decode(request.query)
	query = keys.get("query")
	max_ = keys.get("max")
	if max_ is not None: max_ = int(max_)
	query = query.lower()

	artists = db_search(query,type="ARTIST")
	tracks = db_search(query,type="TRACK")



	# if the string begins with the query it's a better match, if a word in it begins with it, still good
	# also, shorter is better (because longer titles would be easier to further specify)
	artists.sort(key=lambda x: ((0 if x.lower().startswith(query) else 1 if " " + query in x.lower() else 2),len(x)))
	tracks.sort(key=lambda x: ((0 if x["title"].lower().startswith(query) else 1 if " " + query in x["title"].lower() else 2),len(x["title"])))

	# add links
	artists_result = []
	for a in artists:
		result = {"name":a}
		result["link"] = "/artist?" + compose_querystring(internal_to_uri({"artist":a}))
		result["image"] = "/image?" + compose_querystring(internal_to_uri({"artist":a}))
		artists_result.append(result)

	tracks_result = []
	for t in tracks:
		result = t
		result["link"] = "/track?" + compose_querystring(internal_to_uri({"track":t}))
		result["image"] = "/image?" + compose_querystring(internal_to_uri({"track":t}))
		tracks_result.append(result)

	return {"artists":artists_result[:max_],"tracks":tracks_result[:max_]}

####
## Server operation
####



# Starts the server
def start_db():
	log("Starting database...")
	global lastsync
	lastsync = int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp())
	build_db()
	loadAPIkeys()
	#run(dbserver, host='::', port=PORT, server='waitress')
	log("Database reachable!")

def build_db():


	log("Building database...")

	global SCROBBLES, ARTISTS, TRACKS
	global SCROBBLESDICT, STAMPS

	SCROBBLES = []
	ARTISTS = []
	TRACKS = []
	STAMPS = []
	SCROBBLESDICT = {}


	# parse files
	db = tsv.parse_all("scrobbles","int","string","string",comments=False)
	#db = parseAllTSV("scrobbles","int","string","string",escape=False)
	for sc in db:
		artists = sc[1].split("␟")
		title = sc[2]
		time = sc[0]

		readScrobble(artists,title,time)


	# optimize database
	SCROBBLES.sort(key = lambda tup: tup[1])
	#SCROBBLESDICT = {obj[1]:obj for obj in SCROBBLES}
	STAMPS = [t for t in SCROBBLESDICT]
	STAMPS.sort()

	# inform malojatime module about earliest scrobble
	if len(STAMPS) > 0: register_scrobbletime(STAMPS[0])

	# NOT NEEDED BECAUSE WE DO THAT ON ADDING EVERY ARTIST ANYWAY
	# get extra artists with no real scrobbles from countas rules
	#for artist in coa.getAllArtists():
	#for artist in coa.getCreditedList(ARTISTS):
	#	if artist not in ARTISTS:
	#		log(artist + " is added to database because of countas rules",module="debug")
	#		ARTISTS.append(artist)
	# coa.updateIDs(ARTISTS)

	#start regular tasks
	update_medals()

	global db_rulestate
	db_rulestate = consistentRulestate("scrobbles",cla.checksums)

	log("Database fully built!")



# Saves all cached entries to disk
def sync():

	# all entries by file collected
	# so we don't open the same file for every entry
	entries = {}

	for idx in range(len(SCROBBLES)):
		if not SCROBBLES[idx][2]:

			t = get_scrobble_dict(SCROBBLES[idx])

			artistlist = list(t["artists"])
			artistlist.sort() #we want the order of artists to be deterministic so when we update files with new rules a diff can see what has actually been changed
			artistss = "␟".join(artistlist)
			timestamp = datetime.date.fromtimestamp(t["time"])

			entry = [str(t["time"]),artistss,t["title"]]

			monthcode = str(timestamp.year) + "_" + str(timestamp.month)
			entries.setdefault(monthcode,[]).append(entry) #i feckin love the setdefault function

			SCROBBLES[idx] = (SCROBBLES[idx][0],SCROBBLES[idx][1],True)

	for e in entries:
		tsv.add_entries("scrobbles/" + e + ".tsv",entries[e],comments=False)
		#addEntries("scrobbles/" + e + ".tsv",entries[e],escape=False)
		combineChecksums("scrobbles/" + e + ".tsv",cla.checksums)


	global lastsync
	lastsync = int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp())
	log("Database saved to disk.")

	# save cached images
	#saveCache()



###
## Caches in front of DB
## the volatile caches are intended mainly for excessive site navigation during one session
## the permanent caches are there to save data that is hard to calculate and never changes (old charts)
###


import copy

cache_query = {}
if doreah.version >= (0,7,1) and settings.get_settings("EXPERIMENTAL_FEATURES"):
	cache_query_permanent = DiskDict(name="dbquery",folder="cache",maxmemory=1024*1024*500,maxstorage=1024*1024*settings.get_settings("DB_CACHE_SIZE"))
else:
	cache_query_permanent = Cache(maxmemory=1024*1024*500)
cacheday = (0,0,0)
def db_query(**kwargs):
	check_cache_age()
	global cache_query, cache_query_permanent
	key = serialize(kwargs)
	if "timerange" in kwargs and not kwargs["timerange"].active():
		if key in cache_query_permanent:
			#print("Hit")
			return copy.copy(cache_query_permanent.get(key))
		#print("Miss")
		result = db_query_full(**kwargs)
		cache_query_permanent.add(key,copy.copy(result))
		#print(cache_query_permanent.cache)
	else:
		#print("I guess they never miss huh")
		if key in cache_query: return copy.copy(cache_query[key])
		result = db_query_full(**kwargs)
		cache_query[key] = copy.copy(result)

	return result

cache_aggregate = {}
if doreah.version >= (0,7,1) and settings.get_settings("EXPERIMENTAL_FEATURES"):
	cache_aggregate_permanent = DiskDict(name="dbaggregate",folder="cache",maxmemory=1024*1024*500,maxstorage=1024*1024*settings.get_settings("DB_CACHE_SIZE"))
else:
	cache_aggregate_permanent = Cache(maxmemory=1024*1024*500)
def db_aggregate(**kwargs):
	check_cache_age()
	global cache_aggregate, cache_aggregate_permanent
	key = serialize(kwargs)
	if "timerange" in kwargs and not kwargs["timerange"].active():
		if key in cache_aggregate_permanent: return copy.copy(cache_aggregate_permanent.get(key))
		result = db_aggregate_full(**kwargs)
		cache_aggregate_permanent.add(key,copy.copy(result))
	else:
		if key in cache_aggregate: return copy.copy(cache_aggregate[key])
		result = db_aggregate_full(**kwargs)
		cache_aggregate[key] = copy.copy(result)

	return result

def invalidate_caches():
	global cache_query, cache_aggregate
	cache_query = {}
	cache_aggregate = {}

	now = datetime.datetime.utcnow()
	global cacheday
	cacheday = (now.year,now.month,now.day)

	log("Database caches invalidated.")

def check_cache_age():
	now = datetime.datetime.utcnow()
	global cacheday
	if cacheday != (now.year,now.month,now.day): invalidate_caches()


####
## Database queries
####



# Queries the database
def db_query_full(artist=None,artists=None,title=None,track=None,since=None,to=None,within=None,timerange=None,associated=False,max_=None):

	(since, to) = time_stamps(since=since,to=to,within=within,range=timerange)

	# this is not meant as a search function. we *can* query the db with a string, but it only works if it matches exactly
	# if a title is specified, we assume that a specific track (with the exact artist combination) is requested
	# if not, duplicate artist arguments are ignored

	#artist = None

	if artist is not None and isinstance(artist,str):
		artist = ARTISTS.index(artist)

	# artists to numbers
	if artists is not None:
		artists = set([(ARTISTS.index(a) if isinstance(a,str) else a) for a in artists])

	# track to number
	if track is not None and isinstance(track,dict):
		trackartists = set([(ARTISTS.index(a) if isinstance(a,str) else a) for a in track["artists"]])
		track = TRACKS.index((frozenset(trackartists),track["title"]))
		artists = None

	#check if track is requested via title
	if title!=None and track==None:
		track = TRACKS.index((frozenset(artists),title))
		artists = None

	# if we're not looking for a track (either directly or per title artist arguments, which is converted to track above)
	# we only need one artist
	elif artist is None and track is None and artists is not None and len(artists) != 0:
		artist = artists.pop()


	# db query always reverse by default

	result = []

	i = 0
	for s in scrobbles_in_range(since,to,reverse=True):
		if i == max_: break
		if (track is None or s[0] == track) and (artist is None or artist in TRACKS[s[0]][0] or associated and artist in coa.getCreditedList(TRACKS[s[0]][0])):
			result.append(get_scrobble_dict(s))
			i += 1

	return result

	# pointless to check for artist when track is checked because every track has a fixed set of artists, but it's more elegant this way


# Queries that... well... aggregate
def db_aggregate_full(by=None,since=None,to=None,within=None,timerange=None,artist=None):


	(since, to) = time_stamps(since=since,to=to,within=within,range=timerange)

	if isinstance(artist, str):
		artist = ARTISTS.index(artist)

	if (by=="ARTIST"):
		#this is probably a really bad idea
		#for a in ARTISTS:
		#	num = len(db_query(artist=a,since=since,to=to))
		#

		# alright let's try for real
		charts = {}
		#for s in [scr for scr in SCROBBLES if since < scr[1] < to]:
		for s in scrobbles_in_range(since,to):
			artists = TRACKS[s[0]][0]
			for a in coa.getCreditedList(artists):
				# this either creates the new entry or increments the existing one
				charts[a] = charts.setdefault(a,0) + 1

		ls = [{"artist":get_artist_dict(ARTISTS[a]),"scrobbles":charts[a],"counting":[arti for arti in coa.getAllAssociated(ARTISTS[a]) if arti in ARTISTS]} for a in charts]
		ls.sort(key=lambda k:k["scrobbles"],reverse=True)
		# add ranks
		for rnk in range(len(ls)):
			if rnk == 0 or ls[rnk]["scrobbles"] < ls[rnk-1]["scrobbles"]:
				ls[rnk]["rank"] = rnk + 1
			else:
				ls[rnk]["rank"] = ls[rnk-1]["rank"]
		return ls

	elif (by=="TRACK"):
		charts = {}
		#for s in [scr for scr in SCROBBLES if since < scr[1] < to and (artist==None or (artist in TRACKS[scr[0]][0]))]:
		for s in [scr for scr in scrobbles_in_range(since,to) if (artist is None or (artist in TRACKS[scr[0]][0]))]:
			track = s[0]
			# this either creates the new entry or increments the existing one
			charts[track] = charts.setdefault(track,0) + 1

		ls = [{"track":get_track_dict(TRACKS[t]),"scrobbles":charts[t]} for t in charts]
		ls.sort(key=lambda k:k["scrobbles"],reverse=True)
		# add ranks
		for rnk in range(len(ls)):
			if rnk == 0 or ls[rnk]["scrobbles"] < ls[rnk-1]["scrobbles"]:
				ls[rnk]["rank"] = rnk + 1
			else:
				ls[rnk]["rank"] = ls[rnk-1]["rank"]
		return ls

	else:
		#return len([scr for scr in SCROBBLES if since < scr[1] < to])
		return len(list(scrobbles_in_range(since,to)))


# Search for strings
def db_search(query,type=None):
	if type=="ARTIST":
		results = []
		for a in ARTISTS:
			#if query.lower() in a.lower():
			if simplestr(query) in simplestr(a):
				results.append(a)

	if type=="TRACK":
		results = []
		for t in TRACKS:
			#if query.lower() in t[1].lower():
			if simplestr(query) in simplestr(t[1]):
				results.append(get_track_dict(t))

	return results


####
## Useful functions
####

# makes a string usable for searching (special characters are blanks, accents and stuff replaced with their real part)
def simplestr(input,ignorecapitalization=True):
	norm = unicodedata.normalize("NFKD",input)
	norm = [c for c in norm if not unicodedata.combining(c)]
	norm = [c if len(c.encode())==1 else " " for c in norm]
	clear = ''.join(c for c in norm)
	if ignorecapitalization: clear = clear.lower()
	return clear



#def getArtistId(nameorid):
#	if isinstance(nameorid,int):
#		return nameorid
#	else:
#		try:
#			return ARTISTS.index(nameorid)
#		except:
#			return -1


def insert(list_,item,key=lambda x:x):
	i = 0
	while len(list_) > i:
		if key(list_[i]) > key(item):
			list_.insert(i,item)
			return i
		i += 1

	list_.append(item)
	return i


def scrobbles_in_range(start,end,reverse=False):
	if reverse:
		for stamp in reversed(STAMPS):
			#print("Checking " + str(stamp))
			if stamp < start: return
			if stamp > end: continue
			yield SCROBBLESDICT[stamp]
	else:
		for stamp in STAMPS:
			#print("Checking " + str(stamp))
			if stamp < start: continue
			if stamp > end: return
			yield SCROBBLESDICT[stamp]


# for performance testing
def generateStuff(num=0,pertrack=0,mult=0):
	import random
	for i in range(num):
		track = random.choice(TRACKS)
		t = get_track_dict(track)
		time = random.randint(STAMPS[0],STAMPS[-1])
		createScrobble(t["artists"],t["title"],time,volatile=True)

	for track in TRACKS:
		t = get_track_dict(track)
		for i in range(pertrack):
			time = random.randint(STAMPS[0],STAMPS[-1])
			createScrobble(t["artists"],t["title"],time,volatile=True)

	for scrobble in SCROBBLES:
		s = get_scrobble_dict(scrobble)
		for i in range(mult):
			createScrobble(s["artists"],s["title"],s["time"] - i*500,volatile=True)

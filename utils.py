import datetime
from hashlib import md5
import itertools
import json
import os
import psycopg2
import random
import select
import shared
import shutil
import subprocess
from threading import local
import time
import uuid

####################
# FORMATTING UTILS #
####################

def getThreadId():
    if hasattr(tls, "number"):
        return f" \033[34m[Thread {tls.batch}|{tls.number:02d}]\033[0m"
    return ""

def getTimeStamp():
    return datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")

def error(*msg, kill=False):
    print("\033[31m[ ERROR ]\033[0m \033[32m" + getTimeStamp() + "\033[0m" + getThreadId(), *msg, flush=True)
    if(kill):
        exit(1)

def debug(*msg, multiline=False, level=3):
    if level <= shared.DEBUG_LEVEL:
        print("\033[33m[DEBUG-" + str(level) + "]\033[0m \033[32m" + getTimeStamp() + "\033[0m" + getThreadId() + ("\n" if multiline else ""), *msg, flush=True)

def info(*msg):
    print("\033[36m[ INFO  ]\033[0m \033[32m" + getTimeStamp() + "\033[0m" + getThreadId(), *msg, flush=True)

##################
# WORKLOAD UTILS #
##################

def runWorkload(port, seed=None, makeLog=False, logPoll=None, logPipe=None):
    """runs a workload on a given postgres port
    
    Arguments:
    port                        (required) - port the db is listening on
    seed                        (optional) - seed for the RNG. if missing, on will be generated from teh system time.
    makeLog                     (optional) - if set to True, all opens, statements, rollbacks and commits will be logged and returned (default: False)
    logPoll, logPipe            (optional) - poll and pipe as returned by openReader(). Required iff makeLog == True
    
    All other parameters are passed via the shared module
    """
    # TODO: document following shared inputs
    """
    Given via shared module:
    transactions                (required) - number of transactions
    concurrentConnections       (optional) - mean and std dev for how many connections should be concurrently open (default: (3.0, 1.0))
    transactionSize             (optional) - mean and std dev for how many data items should be inserted / updated / deleted (default: 100.0, 5.0)
    statementsPerTransaction    (optional) - mean and std dev for how many statements should be run per transaction (default: (5.0, 1.0))
    pCommitted                  (optional) - probability that transactions end in commit (1 - p for rollback) (default: 0.7)
    pInsert                     (optional) - probability that statements will be insert (default: 0.7)
    pUpdate                     (optional) - probability that statements will be update (default: 0.2)
                                            -> probability for delete will be 1 - pInsert - pUpdate (default: 0.1)
    """
    debug("running workload on port", port, level=2)
    seedMissing = seed is None
    if seedMissing:
        seed = int(time.time()) % 100_000
        debug("no seed given", level=3)
    remainingTransactions = shared.NUM_TRANSACTIONS
    debug("seed:", seed, "number of transactions:", remainingTransactions)
    r = random.Random(seed)
    openConns = []
    finishedTransactions = []
    dbContent = []
    lockedItems = set()
    log = []
    (ccMu, ccVar) = shared.CONCURRENT_TRANSACTIONS
    (sMu, sVar) = shared.STATEMENT_SIZE
    (tMu, tVar) = shared.TRANSACTION_SIZE
    metadata = {
        "transactions": remainingTransactions,
        "seed": seed,
        "seedGiven": not seedMissing,
        "concurrentConnections": { "avg": ccMu, "var": ccVar },
        "transactionSize": { "avg": tMu, "var": tVar },
        "statementSize": { "avg": sMu, "var": sVar },
        "pCommit": shared.P_COMMIT,
        "pRollback": round(1 - shared.P_COMMIT, 5),
        "pInsert": shared.P_INSERT,
        "pUpdate": shared.P_UPDATE,
        "pDelete": round(1 - shared.P_INSERT - shared.P_UPDATE, 5),
        "pSerializationFailure": shared.P_SERIALIZATION_FAILURE,
        "successful": False,
        "numInsert": 0,
        "numUpdate": 0,
        "numDelete": 0,
        "numCommit": 0,
        "numRollback": 0,
        "numCCUpdate": 0,
        "numCCDelete": 0
    }
    cid = 0
    aid = 0
    if makeLog:
        metadata["initialLog"] = [l for l in pollReader(logPoll, logPipe) if "getattr" not in l]
    while len(finishedTransactions) > 0 or len(openConns) > 0 or remainingTransactions > 0:
        
        # debug("currently locked:", lockedItems, level=4)
        
        ccs = r.gauss(ccMu, ccVar)
        
        if (len(openConns) == 0 and len(finishedTransactions) == 0 or ccs > (len(openConns) + len(finishedTransactions)) and hash(ccs) % 2 == 0) and remainingTransactions > 0:
        
            #########################
            # BEGIN NEW TRANSACTION #
            #########################
            
            numStatements = max(round(r.gauss(sMu, sVar)), 1) # at least 1 stmt per transaction
            
            debug("New connection", cid, ",", numStatements, "statements, starting point is", dbContent, level=4)
            
            if makeLog:
                log.append({
                    "type": "open",
                    "transaction": cid,
                    "numStatements": numStatements
                })
            
            try:
                newConn = connect(port)
                newConn.set_session(isolation_level="REPEATABLE READ")
                # with newConn.cursor() as c:
                    # c.execute("BEGIN;")
            except:
                return (dbContent, metadata, log)
            
            openConns.append({"c": newConn, "id": cid, "numStatements": numStatements, "statements": [], "localContent": dbContent.copy(), "lockedVals": set()})
            
            remainingTransactions -= 1
            cid += 1
        
        elif len(finishedTransactions) == 0:
            
            ####################################
            # EXECUTE STATEMENT ON TRANSACTION #
            ####################################
            
            transactionIndex = r.randrange(len(openConns))
            currConn = openConns[transactionIndex]
            stmtTypeP = r.random()
            count = max(1, round(r.gauss(tMu, tVar)))
            
            expectCC = False
            
            if stmtTypeP < shared.P_INSERT or len(set(currConn["localContent"]) - lockedItems) < count:
                
                ##########
                # INSERT #
                ##########
                
                stmtType = "insert"
                metadata["numInsert"] += 1
                values = [(len(currConn["localContent"]) + i, aid) for i in range(count)]
                
                debug("insert", count, "on transaction", currConn["id"], values, level=4)
                
                if makeLog:
                    log.append({
                        "type": "insert",
                        "transaction": currConn["id"],
                        "statement": aid,
                        "count": count,
                        "values": values.copy()
                    })
                
                fun = clientInsert
                args = (values.copy())
                
                try:
                    insert(currConn["c"], values)
                    clientInsert((currConn["localContent"], values))
                except Exception as e:
                    error(type(e), "exception occurred", type(e), e)
                    return (dbContent, metadata, log)
                
            elif stmtTypeP < shared.P_INSERT + shared.P_UPDATE:
                
                ##########
                # UPDATE #
                ##########
                
                stmtType = "update"
                metadata["numUpdate"] += 1
                expectCC = r.random() < shared.P_SERIALIZATION_FAILURE and len(set(currConn["localContent"]) & lockedItems) >= count
                
                debug(len(set(currConn["localContent"]) & lockedItems), level=4)
                
                if expectCC:
                    valsToEdit = [v for v in currConn["localContent"] if v in lockedItems][-count:]
                    metadata["numCCUpdate"] += 1
                    debug("expecting serialization failure", level=4)
                else:
                    valsToEdit = [v for v in currConn["localContent"] if not v in lockedItems][-count:]
                        
                
                debug("update", count, "on transaction", currConn["id"], aid, valsToEdit, level=4)
                
                if makeLog:
                    log.append({
                        "type": "update",
                        "transaction": currConn["id"],
                        "statement": aid,
                        "count": count,
                        "values": valsToEdit.copy()
                    })
                    
                fun = clientUpdate
                args = (valsToEdit.copy(), aid)
                
                try:
                    
                    update(currConn["c"], valsToEdit, aid)
                    if expectCC:
                        error("Expected concurrency conflict")
                        return (dbContent, metadata, log)
                    clientUpdate((currConn["localContent"], (valsToEdit, aid)))
                    currConn["lockedVals"] |= set(valsToEdit)
                    lockedItems |= set(valsToEdit)
                
                except (psycopg2.errors.SerializationFailure, psycopg2.errors.LockNotAvailable):
                    
                    if not expectCC:
                        error("Didn't expect concurrency conflict")
                        return (dbContent, metadata, log)
                    debug("concurrency conflict, need to rollback", level=4)
                    currConn["c"].rollback()
                    currConn["statements"] = []
                    # currConn["localContent"] = currConn          
                    currConn["localContent"] = dbContent.copy()
                    lockedItems -= currConn["lockedVals"]
                    currConn["lockedVals"] = set()
                    aid += 1
                    
                    if makeLog:
                        lazyLog = [l for l in pollReader(logPoll, logPipe) if "getattr" not in l]
                        log.append({"result": "rollback", "logs": lazyLog})
                    
                    continue
                
                except Exception as e:
                    error(type(e), "exception occurred", e)
                    return (dbContent, metadata, log)
                
            else:
                
                ##########
                # DELETE #
                ##########
                
                stmtType = "delete"
                metadata["numDelete"] += 1
                expectCC = r.random() < shared.P_SERIALIZATION_FAILURE and len(set(currConn["localContent"]) & lockedItems) >= count
                
                debug(len(set(currConn["localContent"]) & lockedItems), level=4)
                
                if expectCC:
                    valsToRm = [v for v in currConn["localContent"] if v in lockedItems][-count:]
                    metadata["numCCDelete"] += 1
                    debug("expecting serialization failure", level=4)
                else:
                    valsToRm = [v for v in currConn["localContent"] if not v in lockedItems][-count:]
                
                debug("delete", count, "on transaction", currConn["id"], valsToRm, level=4)
                
                if makeLog:
                    log.append({
                        "type": "delete",
                        "transaction": currConn["id"],
                        "statement": aid,
                        "count": count,
                        "values": valsToRm.copy()
                    })
                
                fun = clientDelete
                args = (valsToRm.copy())
                
                try:
                    
                    delete(currConn["c"], valsToRm)
                    if expectCC:
                        error("Expected concurrency conflict")
                        return (dbContent, metadata, log)
                    clientDelete((currConn["localContent"], valsToRm))
                    currConn["lockedVals"] |= set(valsToRm)
                    lockedItems |= set(valsToRm)
                
                except (psycopg2.errors.SerializationFailure, psycopg2.errors.LockNotAvailable):
                    
                    if not expectCC:
                        error("Didn't expect concurrency conflict")
                        return (dbContent, metadata, log)
                    debug("concurrency conflict, need to rollback", level=4)
                    currConn["c"].rollback()
                    currConn["statements"] = []
                    # currConn["localContent"] = currConn          
                    currConn["localContent"] = dbContent.copy()
                    lockedItems -= currConn["lockedVals"]
                    currConn["lockedVals"] = set()
                    aid += 1
                    
                    if makeLog:
                        lazyLog = [l for l in pollReader(logPoll, logPipe) if "getattr" not in l]
                        log.append({"result": "rollback", "logs": lazyLog})
                    
                    continue
                
                except Exception as e:
                    error(type(e), "exception occurred", e)
                    return (dbContent, metadata, log)
            
            aid = aid + 1
            currConn["statements"].append((fun, args))
            
            debug(stmtType, count, "in transaction", currConn["id"], level=4)
            
            if len(currConn["statements"]) >= currConn["numStatements"]:
                debug("all statements run on conn", currConn["id"], level=4)
                finishedTransactions.append(openConns.pop(transactionIndex))
        
        else:

            ######################
            # FINISH TRANSACTION #
            ######################
            
            transaction = finishedTransactions.pop(r.randrange(len(finishedTransactions)))
            commit = r.random() < shared.P_COMMIT
            
            if commit:
                
                ######################
                # COMMIT TRANSACTION #
                ######################
            
                debug("commit transaction", transaction["id"], level=4)
                metadata["numCommit"] += 1
                
                # lockedItems -= transaction["lockedVals"]
                
                if makeLog:
                    log.append({
                        "type": "commit",
                        "transaction": transaction["id"]
                    })
                
                try:
                    transaction["c"].commit()
                    transaction["c"].close()
                except:
                    return (dbContent, metadata, log)
                for (f, args) in transaction["statements"]:
                    f((dbContent, args))
                    for c in openConns:
                        if len(c["statements"]) == 0: # TODO: COMMIT ONLY INSERTS ON UNTOUCHED CONNS -> BEGIN; CORRECT?
                            f((c["localContent"], args))
            
            else:
            
                ########################
                # ROLLBACK TRANSACTION #
                ########################
            
                debug("rollback transaction", transaction["id"], level=4)
                metadata["numRollback"] += 1
                
                lockedItems -= transaction["lockedVals"]
            
                if makeLog:
                    log.append({
                        "type": "rollback",
                        "transaction": transaction["id"]
                    })
                
                try:
                    transaction["c"].rollback()
                    transaction["c"].close()
                except:
                    return (dbContent, metadata, log)
            
            debug(dbContent, level=4)
            try:
                if not verify(shared.DB_TABLENAME, dbContent, port):
                    return (dbContent, metadata, log)
            except:
                return (dbContent, metadata, log)
        
        #########################
        # LOG SUCCESSFUL ACTION #
        #########################
        
        if makeLog:
            lazyLog = [l for l in pollReader(logPoll, logPipe) if "getattr" not in l]
            log.append({"result": "success", "logs": lazyLog})

    ###############################
    # ALL TRANSACTIONS SUCCESSFUL #
    ###############################

    metadata["successful"] = True
    return (dbContent, metadata, log)

#####################
# SQL CONTROL UTILS #
#####################

def waitUntilAvailable(id, port, timeout=0):
    available = False
    secs = 0
    while not available:
        if shared.SUT == "postgres":
            logs = "\n".join(readLogs(id, "postgres")[-20:])
            if "database system is ready to accept connections" in logs:
                return
        else:
            try:
                c = connect(port)
                c.close()
                return
            except:
                pass
        if timeout != 0 and secs >= timeout:
            error("Timeout while waiting for system start after", timeout, "seconds", kill=True)
        secs += 1
        sleep(1)

def connect(port):
    if shared.SUT == "postgres":
        return psycopg2.connect(user="postgres", host="localhost", port=port)
    else:
        return psycopg2.connect(database="postgres", user="postgres", password="postgres", host="localhost", port=port)

def create(name, schema, port):
    debug("creating db", level=2)
    with connect(port) as conn:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS " + name + ";")
        cur.execute("CREATE TABLE " + name + "(" + ", ".join([s[0] + " " + s[1] for s in schema]) + ");")
        conn.commit()
    debug("\033[1mdone\033[0m creating db")

def insert(conn, values):
    cur = conn.cursor()
    cur.execute("INSERT INTO " + shared.DB_TABLENAME + " VALUES " + ', '.join(str(v) for v in values) + ";")
    debug(cur.rowcount, level=4)

def clientInsert(args):
    (content, values) = args
    content[len(content):] = values

def update(conn, vals, newAction):
    cur = conn.cursor()
    if shared.SUT == "postgres":
        cur.execute("SELECT * FROM " + shared.DB_TABLENAME + " WHERE " + " or ".join(["(a = " + str(a) + " and b = " + str(b) + ")" for (a, b) in vals]) + " FOR UPDATE NOWAIT;")
        cur.execute("UPDATE " + shared.DB_TABLENAME + " SET b = " + str(newAction) + " WHERE " + " or ".join(["(a = " + str(a) + " and b = " + str(b) + ")" for (a, b) in vals]) + ";")
    else:
        cur.execute("UPDATE " + shared.DB_TABLENAME + " SET b = " + str(newAction) + " WHERE " + " or ".join(["(a = " + str(a) + " and b = " + str(b) + ")" for (a, b) in vals]) + ";")
    debug(cur.rowcount, level=4)

def clientUpdate(args):
    (content, vals) = args
    (valsToEdit, newAction) = vals
    for i in range(len(content)):
        (a, b) = content[i]
        if (a, b) in valsToEdit:
            content[i] = (a, newAction)

def delete(conn, vals):
    cur = conn.cursor()
    if shared.SUT == "postgres":
        cur.execute("SELECT * FROM " + shared.DB_TABLENAME + " WHERE " + " or ".join(["(a = " + str(a) + " and b = " + str(b) + ")" for (a, b) in vals]) + " FOR UPDATE NOWAIT;")
        cur.execute("DELETE FROM " + shared.DB_TABLENAME + " WHERE" + " or ".join(["(a = " + str(a) + " and b = " + str(b) + ")" for (a, b) in vals]) + ";")
    else:
        cur.execute("DELETE FROM " + shared.DB_TABLENAME + " WHERE" + " or ".join(["(a = " + str(a) + " and b = " + str(b) + ")" for (a, b) in vals]) + ";")
    debug(cur.rowcount, level=4)

def clientDelete(args):
    (content, valsToRm) = args
    for v in valsToRm:
        while v in content:
            content.remove(v)

def dump(name, port):
    with connect(port) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM " + name + ";")
        return cur.fetchall()

def verify(name, content, port, kill=False):
    debug("verifying", name, level=2)
    data = dump(name, port)
    content = set(str(c) for c in content)
    data = set(str(c) for c in data)
    cross = data ^ content
    if len(content) != len(data):
        error("verify: length mismatch:", len(content), " (local) vs", len(data), "(db)", kill=False)
        error("db has", str(data - content), kill=False)
        error("local has", str(content - data), kill=kill)
        return False
    if len(cross) != 0:
        error("verify: mismatch:", str(cross), kill=False)
        error("db has", str(data - content), kill=False)
        error("local has", str(content - data), kill=kill)
        return False
    debug("\033[1mdone\033[0m verifying", level=2)
    return True

###############################
# SUT CONTAINER CONTROL UTILS #
###############################

def buildSUTImage(wal_sync_method=None):
    debug("building SUT, " + "no WAL_SYNC_METHOD given" if wal_sync_method is None else "WAL_SYNC_METHOD is " + wal_sync_method, level=2)
    r = subprocess.run(["bash", "./build-image.sh", ("" if wal_sync_method is None else wal_sync_method)], cwd=os.sep.join([os.path.dirname(os.path.abspath(__file__)), "SUT", shared.SUT, "scripts"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        error("building SUT failed with code", r.returncode)
        error(r.stdout)
        error(r.stderr, kill=True)
    debug("\033[1mdone\033[0m building", level=2)

def prepHostEnvironment(containerID=None):
    debug("preparing host environment", level=2)
    if containerID is None:
        containerID = str(uuid.uuid4())
        debug("no container ID given, generated ID " + containerID, level=3)
    r = subprocess.run(["bash", "./prep-env.sh", containerID], cwd=os.sep.join([os.path.dirname(os.path.abspath(__file__)), "SUT", shared.SUT, "scripts"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        error("preparing host environment failed with code", r.returncode)
        error(r.stdout)
        error(r.stderr, kill=True)
    debug("\033[1mdone\033[0m preparing env", level=2)
    return containerID

def runContainer(containerID, port=0, crashcmd=""):
    debug("running container", containerID, level=2)
    r = subprocess.run(["bash", "./run-container.sh", containerID, str(port), crashcmd], cwd=os.sep.join([os.path.dirname(os.path.abspath(__file__)), "SUT", shared.SUT, "scripts"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        error("running container failed with code", r.returncode)
        error(r.stdout)
        error(r.stderr, kill=True)
    if port == 0:
        debug("No port given", level=3)
        port = getPort(containerID)
    debug("\033[1mdone\033[0m running", level=2)
    return port

def stopSUT(containerID):
    debug("stopping SUT", containerID, level=2)
    r = subprocess.run(["bash", "./stop-sut.sh", containerID], cwd=os.sep.join([os.path.dirname(os.path.abspath(__file__)), "SUT", shared.SUT, "scripts"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        error("stopping SUT failed with code", r.returncode)
        error(r.stdout)
        error(r.stderr, kill=True)
    debug("\033[1mdone\033[0m stopping SUT", level=2)

def stopContainer(containerID):
    debug("stopping container", containerID, level=2)
    r = subprocess.run(["bash", "./stop-container.sh", containerID], cwd=os.sep.join([os.path.dirname(os.path.abspath(__file__)), "SUT", shared.SUT, "scripts"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        error("stopping container failed with code", r.returncode)
        error(r.stdout)
        error(r.stderr, kill=True)
    debug("\033[1mdone\033[0m stopping container", level=2)

def cleanupEnv(containerID):
    debug("cleaning up host environment", containerID, level=2)
    r = subprocess.run(["bash", "./cleanup-env.sh", containerID], cwd=os.sep.join([os.path.dirname(os.path.abspath(__file__)), "SUT", shared.SUT, "scripts"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        error("cleaning up host environment failed with code", r.returncode)
        error(r.stdout)
        error(r.stderr, kill=True)
    debug("\033[1mdone\033[0m cleaning up env", level=2)

def cleanupContainer(containerID):
    stopContainer(containerID)
    cleanupEnv(containerID)

def cleanupEnvs():
    debug("cleaning up all envs", level=2)
    r = subprocess.run(["bash", "./cleanup-envs.sh"], cwd=os.sep.join([os.path.dirname(os.path.abspath(__file__)), "SUT", shared.SUT, "scripts"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        error("cleaning up all envs failed with code", r.returncode)
        error(r.stdout)
        error(r.stderr, kill=True)
    debug("\033[1mdone\033[0m cleaning up", level=2)

def cleanupAll():
    debug("cleaning up all", level=2)
    r = subprocess.run(["bash", "./cleanup-all.sh"], cwd=os.sep.join([os.path.dirname(os.path.abspath(__file__)), "SUT", shared.SUT, "scripts"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        error("cleaning up all failed with code", r.returncode)
        error(r.stdout)
        error(r.stderr, kill=True)
    debug("\033[1mdone\033[0m cleaning up", level=2)

def getPort(containerID):
    port = 0
    tries = 1
    while port == 0:
        result = subprocess.check_output("docker ps", shell=True).decode().strip()
        debug("Looking for container port", level=3)
        if tries >= 10:
            debug("this is attempt", tries, level=3)
        debug("Got output\n", result, level=4)
        if containerID in result:
            lines = result.splitlines()
            for l in lines:
                if containerID in l:
                    port = int(l.split("0.0.0.0:")[1].split("->5432")[0])
                    debug("got port", port, level=3)
                    return port
        else:
            if tries == 20:
                error("Container port for", containerID, "not found after 20 tries, likely didn't start.")
                exit(1)
            tries += 1
            time.sleep(0.5)

######################
# FILE CONTROL UTILS #
######################

def commandIntoFifo(containerID, cmd):
    debug("writing into fifo for container", containerID, cmd, level=2)
    with open(os.sep.join(["SUT", shared.SUT, "container", "container-" + containerID, "faults.fifo"]), "w") as fifo:
        fifo.write(cmd + "\n")
    debug("\033[1mdone\033[0m writing", level=2)

def readLogs(containerID, name):
    debug("reading logs", name, "of container", containerID, level=2)
    with open(os.sep.join(["SUT", shared.SUT, "container", "container-" + containerID, name + ".log"])) as log:
        logs = log.readlines()
    debug("\033[1mdone\033[0m reading logs", level=2)
    return logs

def openReader(containerID, name):
    debug("opening reader on logs", name, "of container", containerID, level=2)
    f = subprocess.Popen(["tail", "-f", "/".join(["SUT", shared.SUT, "container", "container-" + containerID, name + ".log"])], stdout=subprocess.PIPE)
    p = select.poll()
    p.register(f.stdout)
    debug("\033[1mdone\033[0m opening reader", level=2)
    return (p, f)

def pollReader(poll, pipe):
    debug("polling log", level=2)
    l = []
    while poll.poll(1):
        l.append(pipe.stdout.readline().decode().strip())
    debug("got", len(l), "lines")
    debug("\033[1mdone\033[0m polling log", level=2)
    return l

def closeReader(pipe):
    if pipe is not None:
        pipe.kill()

def dumpIntoFile(path, content, force=False):
    if force:
        with open(path, "w") as f:
            f.write(content)
    else:
        with open(path, "x") as f:
            f.write(content)

##############
# MISC UTILS #
##############

tls = local()

def sleep(secs):
    debug(f"sleeping for {secs} seconds", level=2)
    time.sleep(secs)

def traceHash(log):
    newLog = []
    # filter out lazyfs logs
    for item in log:
        if "result" in item:
            newLog.append({"result": item["result"]})
        else:
            newLog.append(item)
    return md5(json.dumps(newLog).encode(encoding="utf-8")).hexdigest()

def extractFiles(logs):
    files = {}
    for line in logs:
        if not "[lazyfs.ops]" in line or not "lazyfs.root" in line:
            continue
        path = line.split("lazyfs.root/")[1].split(",")[0].split(")")[0]
        op = line.split("lfs_")[1].split("(path=")[0].split("(")[0]
        if not path in files:
            files[path] = {}
        if not op in files[path]:
            files[path][op] = 0
        files[path][op] += 1
    return files

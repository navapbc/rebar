import os, time, fcntl, sys
STATE=os.path.join(os.path.dirname(__file__) or ".", ".hlc.state")
LOCK=STATE+".lock"
def seed_from_max(prefixes):
    if prefixes:
        with open(STATE,"w") as f: f.write(str(max(prefixes)))
def next_tick():
    fd=os.open(LOCK,os.O_CREAT|os.O_RDWR)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        last=0
        try:
            with open(STATE) as f: last=int(f.read().strip() or 0)
        except FileNotFoundError: pass
        t=max(time.time_ns(), last+1)
        tmp=STATE+f".tmp{os.getpid()}"
        with open(tmp,"w") as f: f.write(str(t))
        os.replace(tmp,STATE)
        return t
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN); os.close(fd)
if __name__=="__main__":
    n=int(sys.argv[1]); 
    vals=[next_tick() for _ in range(n)]
    sys.stdout.write("\n".join(map(str,vals))+"\n")

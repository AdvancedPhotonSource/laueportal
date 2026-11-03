# Live deployment test

A test of live processing on the deployment host, against the facility's own systems, before the beamline relies on it.
The build was tested against a stand-in DM (`FakeDMService`) on a local disk only. This test runs both arrival sources
on the real ones: a DAQ's file catalog in DM, and `laueportal live notify`, with the runs on the facility's runs root
(NFS), where the arrivals lock and the follower's reads meet NFS. It ends with the choice of which source to keep
(either may be removed after this test). `$root` and `$py` are `deploy.md`'s (step 1).

## Before

- [ ] The portal deployed and running (`deploy.md` steps 1-6), live processing on (`[live] enabled = true`, `deploy.md`
      step 6), DM access set (`[live] dm_setup_file`, and `dm_ssh_host` if the DM operations run elsewhere: "DM
      access for live runs"); the api and the worker restarted since.
- [ ] No batch run running, or one that may be stopped: a live session stops it ("stopped for a live run", Cancelled;
      re-run by Clone) and holds the queue until it ends. Tell the users.
- [ ] Test data, copies only: a past scan's raw frames (`<prefix>_%d.h5`, 100 numbered in sequence) with its Geometry
      File and Structures File, and a wire scan's stacks (`<wire prefix>_%d.h5`, 10 points) with its Geometry File.
- [ ] A DM DAQ running for a test experiment, watching a fresh folder that the portal host sees at DM's
      `storageDirectory` (DM's administrators start and stop it).
- [ ] For the replays (steps 2 and 3), a scratch folder beside each destination folder (outside it, on its file
      system): each file is copied there first, then hard-linked in under its name (`ln`: the same file system, and it
      refuses a name that exists), so that it appears whole. Nothing else is needed: `cp`, `ln` and `sleep`.
- [ ] A shell on the portal host with `. "$root/config/prod.env"`.
- [ ] The acquisition hook's key installed (`deploy.md`, "The acquisition hook's key"), and a shell on the
      acquisition host as the hook's account.

## 1. The clocks

The portal's clock must be within 120 s of DM's (NTP on both). Until a DAQ run's first file is catalogued, its catalog
queries start 120 s before the earlier of the portal's clock at its last full scan and the folder's newest catalogued
time: with the portal's clock further ahead, the run's first files wait for the next full re-scan (`[live]
catalog_rescan_seconds`, 300 s). None is lost, but they come up to five minutes late.

```bash
timedatectl show -p NTPSynchronized --value                       # yes
date -u +%s; ssh -o BatchMode=yes <dm_ssh_host> date -u +%s       # with dm_ssh_host set: within 120 of each other
```

- [ ] The portal host is synchronized, and DM's hosts are too (ask DM's administrators: the catalog's times are its
      services').
- [ ] In step 2, the first frame is counted within about half a minute of the replay's first file (DM's catalog,
      then a cycle); a first file that takes about five minutes points at the clocks.

## 2. A DAQ session: indexing raw frames from DM's catalog

1. On the Live page: **Source** the test DAQ (Folder fills with where it writes), **Filename Template**
   `<prefix>_%d.h5`, **Output Folder** under the runs root, a Scan Number and Author; Reconstruction off, Indexing on
   with the **Geometry File** and the **Structures File**. **Validate**, then **Start**.
2. Replay the frames into the DAQ's folder, about two per second (the loop stops at a file it cannot copy or link):

   ```bash
   src=<frames folder>; daq=<the DAQ's folder>; part=<its scratch folder>/replay.part
   for n in $(seq <first frame number> <last frame number>); do   # the 100 frames
     rm -f "$part"   # an interrupted run may have left it linked to a delivered frame, which cp would rewrite
     cp "$src/<prefix>_$n.h5" "$part" && ln "$part" "$daq/<prefix>_$n.h5" && rm "$part" || break
     sleep 0.5
   done
   ```

3. Look at:
   - [ ] the Live page's header: **Arrived** follows the replay within a few seconds (a cycle every 5 s while files
         arrive; up to 30 s after a pause), **Done** follows it, **Failed** stays 0; **Arrivals** shows the last
         check, current;
   - [ ] the run's job page, **Live Run**: Source the DAQ, **Last Check** moving, **Rows Seen** growing, no **Error**;
   - [ ] the indexing viewer: new results every few seconds, the step and the view kept;
   - [ ] `arrivals.jsonl` in the run's folder (`<Output Folder>/index_<ID>`): every file once, `n` in order:

     ```bash
     "$py" - <run folder>/arrivals.jsonl <<'EOF'
     import json, sys
     lines = [json.loads(line) for line in open(sys.argv[1])]
     print(len(lines), "lines,", len({line["path"] for line in lines}), "files,",
           "n in order" if [line["n"] for line in lines] == list(range(len(lines))) else "n NOT in order")
     EOF
     ```

   - [ ] the job log (`$root/logs/jobs.log`, the job processes' own): no catalog errors.
4. **Stop**: Stopping, then no session on the Live page (Idle); in the Run Monitor, the run Finished with Done equal
   to Arrived (every frame taken is finished; none after the Stop).
5. Optional: a batch indexing run (New Index) of the same folder with the same parameters: the same frames indexed,
   the same peaks (live runs keep arrival order, matched to batch by scan point).

## 3. A notify session: a wire reconstruction followed by its indexing

1. On the Live page: **Source** Notifications, **Folder** a fresh folder on the facility's storage, **Filename
   Template** `<wire prefix>_%d.h5`, **Output Folder** under the runs root; Reconstruction and Indexing on (Indexing's
   Depth Indices as usual). **Validate**, **Start**; note the reconstruction's job ID.
2. Replay the stacks the same way, one every ten seconds, and notify each one twice at once, as a hook that retries
   would (two writers of the arrivals at the same moment, under the arrivals lock on NFS):

   ```bash
   job=<the reconstruction's job ID>; folder=<the Folder>; src=<stacks folder>; part=<its scratch folder>/replay.part
   for n in <the 10 point numbers>; do
     file="$folder/<wire prefix>_$n.h5"
     rm -f "$part"   # as above
     cp "$src/<wire prefix>_$n.h5" "$part" && ln "$part" "$file" && rm "$part" || break
     laueportal live notify "$job" "$file" --complete & a=$!
     laueportal live notify "$job" "$file" --complete & b=$!
     wait "$a" "$b"
     sleep 10
   done
   ```

3. Look at:
   - [ ] each pair of notify lines: one `"admitted": true`, the other `false` (a no-op); never an error. A
         `failed: OSError: ...` names the lock: the runs root's mount must take locks (`findmnt -T <runs root> -o
         OPTIONS`: with `nolock` the lock is this host's alone, which is enough, as every writer runs on the portal
         host; otherwise the server's lock manager must answer). A lock that cannot be taken admits nothing, and the
         run fails at its seal;
   - [ ] once, through the hook's key from the acquisition host, with a stack the loop notified (a repeat):
         `ssh -i ~/.ssh/laue_notify -o BatchMode=yes <portal account>@<portal host> laueportal live notify <job ID>
         <its file> --complete` prints the same JSON line, `"admitted": false`, and exits 0; the same key with `id`
         in place of the command prints `laueportal notify hook: refused: ...` and exits 2;
   - [ ] the reconstruction's `arrivals.jsonl` (`<Output Folder>/rec_<ID>`): each stack once, `n` in order (the
         check in step 2);
   - [ ] the wire viewer: **Follow newest point** shows each point as it completes;
   - [ ] the indexing's job page (it follows the reconstruction): **Points Read** reaches the reconstruction's
         completed points within about a minute (on NFS a stat can show a file's old size for the mount's attribute
         cache time, `acregmax`, 60 s by default), **Waiting** empty, **Arrivals** the points;
   - [ ] **Stop** on the Live page: Stopping until both have ended, then no session on the page (Idle); in the Run
         Monitor, the reconstruction Finished, then the indexing Finished once it has indexed the last points.
4. Optional: batch runs of the same stacks (New Recon, then New Index of it) give the same points and indexed depths.

## 4. A hung input (if it happens; do not provoke it on the experiment's storage)

A live run has no time limit, so an input whose read hangs on a mount that stops answering keeps it Running
(`deploy.md` §8, "Known limit, live runs").

- **Symptom:** the run's counters stop at an input (a LaueGo run's preview stops at the frame before it; a wire run's
  other points go on), its heartbeat stays fresh, and **Stop** does not end it: it is sealed but cannot drain. No
  batch run or conversion starts meanwhile.
- **Remedy:** the Live page's **Stop now** (Stop, while the session is Stopping): the run is killed and ends Failed
  (interrupted) within a few seconds, its files kept. Without the page: end its job process's group:
  `pgrep -f "laueportal.jobs.process run <job id> --cores"` gives the pid, then `kill -KILL -- -<pid>`. The reconciler
  ends the run Failed (interrupted) once its heartbeat is stale: after up to 6 minutes (`stale_heartbeat_seconds` plus
  `reconcile_seconds`, 300 s + 60 s; the session stays open meanwhile); the files written so far stay. A follower of a
  wire run ended so seals by itself with every point that run completed. Once the mount answers, start a new session for
  the inputs that are left (there is no Resume).
- **The same on the seal's lock:** a runs root whose NFS lock manager stops answering holds Stop at the seal (the
  `flock` on `arrivals.lock` has no time limit), and notify with it; Stop now is the remedy.

## 5. Which source to keep

Either source may be removed after this test. Note for each how it went (every file once, how late the first file and
the others came, any error), then choose one or both:

| | DAQ catalog | `laueportal live notify` |
|---|---|---|
| The acquisition side | nothing: DM's DAQ catalogs the files | a hook that runs `notify <job ID> <file> --complete` on the portal host over SSH once a file is complete, through a key that runs nothing else (`deploy.md`, "The acquisition hook's key") |
| Needs | DM (`dm_setup_file`, its DAQ and catalog services, the storage mounted at its paths) and the clocks within 120 s | the hook and its key, and the job ID of the session's first run |
| How late | a cycle (5 s) while files arrive; up to 30 s after a pause | at once |
| Load on shared systems | one DM call per cycle while a DAQ run is open | none |
| Cost per file on the portal host | none of its own: the job process's catalog thread admits each file (one DM call per cycle in all) | one process per file: 0.69 s CPU and 63 MB (Python, then SQLAlchemy and the settings' pydantic: about 0.55 s of it), whatever the session's length; so about 7 cores at 10 files/s, beside the live runs' share. Its admission holds the run's arrivals lock 15-20 ms (its index beside the database, `jobs.arrival_index`), so the lock serves about 50 files/s |
| Measured (a ledger of 3,600 / 36,000 / 100,000 lines) | — | admission 17 / 15 / 17 ms; the process 0.69 / 0.69 / 0.69 s CPU, 64 / 63 / 64 MB; a run's first notify after its index was lost rebuilds it once (0.15 / 1.3 / 3.4 s) |

The source not kept may be removed from the code later. Until then, a DAQ source needs `dm_setup_file` (empty: none is
offered), and notify is used only by whoever runs it. Record the choice.

## After

- [ ] No session open (the Live page's header), and `[live] enabled` as decided.
- [ ] The DAQ stopped (DM's administrators). The test's runs stay as ordinary runs, with their folders.

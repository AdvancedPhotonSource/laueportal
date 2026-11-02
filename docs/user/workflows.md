# Workflows

Short how-tos for the 3DMN Portal.

## Import scans

1. Open **Menu → Import Scans**.
2. Choose the scan log: **Browse Server Logs** (it opens at the beamline's scan-log folder), or **Upload from
   Computer**.
3. The Scans table lists every scan in the log with its status: New, Exists, Duplicate or Invalid. Only New scans
   can be selected.
4. Check the scans to import, or use **Quick Select**: a scan ID ("Scan ID greater than", then its **Select**) or
   a date ("Created at or after", then its **Select**).
5. Fill in the **Catalog Defaults** if you want them: Aperture, Sample Name, Filename Prefix, the files' folder and
   Notes. They apply to every imported scan.
6. Click **Import Selected**. The summary and each row's status show what was imported.

The scans then appear on the Scans page. A scan's own page lets you edit its catalog (Catalog tab) and add a note
(Scan Info tab).

## Run a wire reconstruction

1. On **Scans**, check the scan and click **New Recon**. (Or open **Menu → New Wire Reconstruction** and pick the
   scan under Source.)
2. The form fills the data fields from the scan: Folder Path, Filename and Output Path.
3. **Find Matching Files** suggests the folder's filename templates; **Find Indices** fills the indices of the
   matching files.
4. Set the parameters. **Restore Default** resets a section.
5. Choose **Run on** (APS, or ALCF when it is set up).
6. The form checks your values as you type; **Validate** checks them at once. Fix the red errors; read the amber
   warnings.
7. Click **Submit**, and confirm if there are warnings. The new run's page (R…) opens; if the run could not be
   queued, the form says why and links to the run and its job.

To repeat a run with its values: on **Reconstructions**, check the run and click **New Wire Recon** (a clone).

## Index a scan or a reconstruction

**A scan:** on **Scans**, check the scan and click **New Index**. The Source is Scan, and the form fills Folder Path,
Filename and Output Path from it.

**A reconstruction:** on **Reconstructions**, check the wire run and click **New Index**. The Source is
Reconstruction: the input is its `scan.h5`, with every depth (leave the depth range blank) or the depths you type.
On a scan's page, the Reconstructions card does the same.

Then set the parameters, choose **Run on**, and **Submit**, as for a reconstruction. The new run is I….

**Clone a run, or clone it onto another scan:**

1. On **Indexations**, check the run and click **New Index**. (Or set the form's Source to **Clone** and pick the
   run.) Its parameters are copied; the author and notes start blank.
2. To use them on another scan, switch the Source to **Scan** and pick that scan. The data fields take the new
   scan's paths; the parameters stay.
3. **Submit**.

## Inspect results

**Indexing results:** open an I… link (Indexations, a scan's page, or the job page).

- **Parameters:** the run's values.
- **Color Map:** the map in 8 colour modes, in 2D or 3D. Hover over a point for its values; click it for its
  details. Adjust the colours on the colorbar: drag its handles, double-click a value to type it.
- **Pole Figure:** set the HKL and click **Update**. Click a pole to centre the colours on it; draw a lasso or box
  to see the misorientation statistics, highlighted on the map.
- **Detector View:** a step's detector image with its overlays (detected, indexed, simulated peaks, hkl labels).
  Previous and Next move through the steps.
- **Peaks** and **Patterns:** the tables, filtered and sorted on the server.
- The scope bar above the tabs limits what is shown (pattern 0 only, a minimum number of peaks).
- If the run has only an Output XML, the bar above offers to convert it to `output.h5`; if its folder has several
  XML files, choose one there.

**Wire reconstruction results:** open an R… link.

- **Points:** check a complete point to view it.
- **Depth View:** move through the depths with the slider, or Previous and Next. Zoom with the mouse; the zoom
  stays when you change depth.
- **ROI Inspector:** choose the reference image and the ROI size, then click the image to add ROIs. The traces show
  each ROI along the depth. Delete an ROI from the list.

Both viewers keep your settings in this browser. **Copy link to this view** copies a link that opens exactly what
you see.

## Run a live session

Open **Live** in the navbar (it shows only while live processing is on; below 1024 px it is the Menu's first item).
One live session runs at a time: its runs process each new file as it arrives, on this host. While it runs, no local
batch run or conversion starts: a batch run running at Start is stopped ("stopped for a live run", Cancelled; re-run
it by Clone), and queued ones wait until the session has ended.

1. **Scan:** pick the **Source**: a DM DAQ from the list (it fills **Folder** with where the DAQ writes; its From and
   To folders show beside it), or **Notifications**, where an acquisition hook runs
   `laueportal live notify <job ID> <file> --complete` for each finished file. Enter **Scan Number**, **Author**,
   **Folder** (where the files arrive), **Filename Template** (one `%d`, each file's number) and **Output Folder**
   (the session's runs go in it as `rec_<ID>` and `index_<ID>`).
2. Switch on **Reconstruction**, **Indexing**, or both. Both: the indexing run indexes each reconstructed point as it
   completes (set its **Depth Indices**; a reconstruction of more than 100,000 depths cannot be followed: its
   indexing fails at the first point, and the reconstruction goes on); Indexing alone indexes each raw frame. Set
   their parameters as on the create forms; Indexing has its **Output XML**.
3. **Validate** checks the form (the DAQ is checked with DM at Start), then **Start**. The form turns read-only and
   the header shows the session: its state, each run with its link and its job's, the files **Arrived**, **Done** and
   **Failed**, and the arrival status (a DAQ's catalog: its last check or why files are not taken yet).
4. **Stop** (it asks first) takes no new file and finishes those taken; the session shows **Stopping** until it ends
   (with both runs, the indexing finishes the last reconstructed points after the reconstruction has ended), then
   **Finished** (**Cancelled** when nothing arrived, **Failed** when a file failed). There is no Resume: the form then
   holds the ended session's values (author and notes reset unless you typed them), so **Start** begins the next
   session from them.
5. While the session is **Stopping**, **Stop** becomes **Stop now** (it asks first): every run still open stops, and
   a run already stopping is ended at once, **Failed**, keeping what it published. Use it when Stopping does not end:
   hover **Stopping** to see what the session waits for (e.g. a reconstructed point the indexing cannot read).

A restart of the portal's worker (an upgrade, a change of its settings) ends an open session: its runs end **Failed**,
with what they published kept. There is no Resume: a new session on a DAQ takes the folder's files again from its
first.

Open the runs from the header while they run: the viewers show what is done so far. Once the session has ended its
runs are ordinary runs: their results in their folders (`rec_<ID>`, `index_<ID>`, the indexing's Output XML written
at the end), in the Run Monitor, the viewers and the job pages; a batch indexing can index the reconstruction (New
Index), as any other's.

- **Wire viewer:** switch on **Follow newest point** (beside Refresh) to show each point as it completes (the last
  complete one in arrival order). Picking a point yourself turns it off.
- **Indexing viewer:** it reads the new results by itself every few seconds; the step and the view you are on stay.
- **Job page:** the **Live Run** card shows the run's **Source** (a DAQ, Notifications, or the reconstruction the
  indexing follows, linked), its **Folder** and **Filename Template**, its **Arrivals** (the files taken so far; the
  points, for the indexing that follows a reconstruction), and its arrival status when it has one: a DAQ's **Last
  Check**, **Rows Seen** and **Error**; the following indexing's **Points Read** and what it is **Waiting** for. Its
  **Stop Run** says what a live run's stop does: no new files are taken; the files taken are finished. On the
  session's first run (the reconstruction, or an Indexing session's indexing) it stops the session, as the Live page's
  Stop does; on the indexing that follows a reconstruction it stops only the indexing, and the reconstruction goes on.

## Manage the queue

Open **Run Monitor**. It lists every job, newest first, and updates by itself.

- **Stop:** check one or more queued or running jobs, click **Stop**, and confirm. A running run finishes the
  inputs in progress; the rest are recorded as not run. A remote run's stop is sent to its target. A live run takes
  no new files and finishes the files taken, as the Live page's Stop does.
- **Held:** a queued run marked **Held** waits for the live session: no other run (nor a conversion to HDF5) starts
  until the session has ended. Its job page says so too.
- **Move to Front:** check queued local jobs, click **Move to Front**, and confirm. They run next, after any live
  run; a live run cannot be moved.
- **Refresh** reloads the list at once.
- Click a job ID for its page: its details, progress, files, failed inputs and log. **Stop Run** is there too.

The status page (`/`) shows the database, the worker and its threads (the dispatcher, the reconciler and the remote
poller: OK, Late or Off), the queue with the next five runs or conversions it starts and the live slots in use (of
two: a live run takes one; while a live run is queued or running, no local batch run or conversion starts, and a
running one is stopped: a run ends Cancelled, re-run it by Clone; a conversion fails, convert again), and the active
runs.

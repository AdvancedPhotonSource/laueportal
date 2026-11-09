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
5. Choose **Run on** (APS, or ALCF when it is set up). Run on, **Validate** and **Submit** are beside the page's
   title, with the validation summary under them; on a wide window they stay in view while you scroll.
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

- **Parameters:** the run's values, in the create form's sections and order.
- **Color Map:** the map in 8 colour modes, in 2D or 3D. By default the 2D view plots X motor against H, and the 3D view
  X lab, Y lab and Z lab, or X motor, Y motor and Z motor when the results have no depths (so no Z lab); choose other
  axes in the sidebar. Hover over a point for its values; click it for its details. Adjust the colours on the colorbar:
  drag its handles, double-click a value to type it.
- **Crystal orientation** (beside the Color Map): the clicked point's unit cell in beamline coordinates (X, Y up, Z
  along the beam), drawn from its pattern's reciprocal lattice (a\*, b\*, c\*). Orientation indexing keeps the
  crystal's nominal cell, so the box is that cell, oriented as indexed; the card shows a **Nominal** row beside a, b,
  c and α, β, γ only when the results hold a refined lattice that differs from it. Drag to turn the view;
  double-click to reset it. The view stays when you click another point, so you can compare orientations. **Beam**
  draws the beam's direction (+Z) from the shared origin of a, b, c. **Rotation** is the rotation from the crystal's
  reference cell (c along Z, b in the YZ plane) about a lab axis; for a cubic or hexagonal crystal it is the smallest
  of its equivalent rotations, as in the Pole Figure's misorientation, and a, b, c are labelled to match. **Beam ∥**
  and **Up ∥** are the crystal directions along the beam and up. A step with several patterns offers each one.
  Collapse the card to a strip with the arrow; the portal keeps your choice.
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
   To folders show beside it; the list leaves out a DAQ whose option, its experiment and the last two folders of its
   data folder, has `logs` or `current` in it, as the setting `[live] daq_name_exclude` says), or **Notifications**,
   where an acquisition hook runs `laueportal live notify <job ID> <file> --complete` for each finished file. Enter
   **Scan Number**, **Author**, **Folder** (where the files arrive; with a DAQ it may not exist yet, a warning: the
   DAQ creates it when it writes), **Filename Template** (one `%d`, each file's number; empty: every file whose name
   ends `.h5`, its number the last number in its name, so `Si_12_3.h5` is 3 and `Si_2_dark.h5` is 2; a `.h5` file
   without a number is not taken, and the run's arrival status on this page says so, for a DAQ and for
   notifications) and **Output Folder** (the session's runs go in it as `rec_<ID>` and `index_<ID>`; Start creates it
   when it does not exist; one that holds anything but earlier sessions' `rec_<ID>` and `index_<ID>` folders is a
   warning).

   **Output Folder** follows **Folder** until you edit it: a Folder whose path has a `data` folder gives the same path
   with the last `data` replaced by `analysis` (`/x/exp/data/scan_12` gives `/x/exp/analysis/scan_12`); a Folder
   without one, or an empty one, gives the configured default. The button beside it, **From data folder**, sets it
   from the Folder again after you edited it. A form filled from the last session keeps following when that
   session's Output Folder was the one its Folder gave; one set by hand stays until you press the button.
2. Switch on **Reconstruction**, **Indexing**, or both. Both: the indexing run indexes each reconstructed point as it
   completes (set its **Depth Indices**; a reconstruction of more than 100,000 depths cannot be followed: its
   indexing fails at the first point, and the reconstruction goes on); Indexing alone indexes each raw frame. Set
   their parameters as on the create forms; Indexing has its **Output XML**.
3. **Validate** checks the form (the DAQ is checked with DM at Start), then **Start**. The form turns read-only and
   the header shows the session: its state, each run with its link and its job's, the files **Arrived**, **Done** and
   **Failed**, and the arrival status (a DAQ's catalog: its last check or why files are not taken yet; a file
   without a number).
4. **Stop** (it asks first) takes no new file and finishes those taken; the session shows **Stopping** until it ends
   (with both runs, the indexing finishes the last reconstructed points after the reconstruction has ended). Once it
   has ended, every run **Finished**, **Cancelled** (when nothing arrived) or ended by **Stop now**, the page shows no
   session (**Idle**): its runs are in the Run Monitor and on their job pages. A session that ends without a Stop, or
   with a run **Failed** otherwise (a file failed, the worker stopped, the indexing could not follow), stays shown with
   its state until the next Start. There is no Resume: the form then holds the ended session's values (author and
   notes reset unless you typed them), checked again, so **Start** begins the next session from them.
5. While the session is **Stopping**, **Stop** becomes **Stop now** (it asks first): every run still open stops, and
   a run already stopping is ended at once, **Failed** in the Run Monitor, keeping what it published; once every run
   has ended the page shows no session, as after Stop (unless a file of that run had failed). Use it when Stopping
   does not end: hover **Stopping** to see what the session waits for (a reconstructed point the indexing that follows
   it cannot read).

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
  indexing follows, linked), its **Folder** and **Filename Template** (— for none), its **Arrivals** (the files taken
  so far; the points, for the indexing that follows a reconstruction), and its arrival status when it has one: a DAQ's
  **Last Check**, **Rows Seen** and **Error**; the following indexing's **Points Read** and what it is **Waiting**
  for; a notification run's **Error**, its last file without a number. Its **Stop Run** says what a live run's stop
  does: no new files are taken; the files taken are finished. On the session's first run (the reconstruction, or an
  Indexing session's indexing) it stops the session, as the Live page's Stop does; on the indexing that follows a
  reconstruction it stops only the indexing, and the reconstruction goes on.

## Manage the queue

The Local dot shows the portal worker's state. When ALCF is enabled, its dot opens a connectivity panel; hovering
shows a compact summary and last-check time. The panel lists DM, Globus,
APS/Eagle storage and the Crux endpoint, with the last-check time and Refresh. Amber can mean Login needed or
stale/partial checks; red means a service is unavailable or the endpoint reports offline. Checks are shared and
run at most once every five minutes, backing off after failures, so Refresh may show the same recent result.
They submit no jobs. An available endpoint does not guarantee an immediate PBS allocation or a successful computation.

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

Beside the portal's name, the navbar shows the worker's state as a link to the status page: a muted green dot while
the worker runs, a grey ring until the API answers the first check, and a tinted label when the worker or the API is
not working:

- **Worker late:** the worker has shown no sign of life for over 45 s.
- **Worker not running:** the worker is stopped; queued runs wait until it starts.
- **Worker unknown:** the database does not answer, so the worker's state cannot be read.
- **API unreachable:** the API does not answer: it is stopped or restarting, the network is down, or the check has
  had no answer for 10 s.
- **API error:** the API answers the check with an error.

Its tooltip shows the details and the time of the last answer. In a window narrower than about 800 px, a label can
drop its first word (Worker or API); the words of the state stay whole.

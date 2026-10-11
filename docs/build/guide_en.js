'use strict'
const L = require('./guide_lib')
const { COVER, H1, H2, H3, P, UL, OL, NOTE, TIP, WARN, CODE, TABLE, IMG, GAP } = L

const SERVER = '\\\\10.104.46.34\\calllog'
const blocks = [
  COVER({
    title: 'NBE Call Log', subtitle: 'User Guide', tagline: 'Setup steps · Employee guide · Administrator guide',
    contents: ['Part 1 – Setup steps', 'Part 2 – Employee guide', 'Part 3 – Administrator guide', 'Part 4 – Troubleshooting'],
    version: 'Version 1.0 · October 2026',
  }),

  H1('About NBE Call Log'),
  P('NBE Call Log replaces the Excel sheet **“Incoming Calls Registration Log”**. Every employee records each incoming call in a web page: the **IP phone** that received the call, the **branch** the call came from (only when the caller is a branch) and the **reason** for the call. Administrators see all employees live, can correct records and can export everything to Excel.'),
  P('There is no database and no server product to buy or install. The program and all its data live in the shared folder `' + SERVER + '`.'),
  IMG('setup-01-architecture.png', 'How the parts fit together'),
  H2('How it works'),
  UL([
    'Employees open a web address in their browser (Edge, Chrome or Firefox). They install nothing.',
    'One Windows PC runs the program (the file `run.py`). The program reads and writes the shared folder.',
    'All data is saved as simple files in `' + SERVER + '\\data`. To back up, copy that folder.',
  ]),
  H2('Which part should I read?'),
  TABLE([3600, 6106], ['If you are…', 'Read'], [
    ['The person who installs the program', '**Part 1 – Setup steps**'],
    ['An employee who answers calls', '**Part 2 – Employee guide** (and print the one-page Quick Card)'],
    ['An administrator or supervisor', '**Part 3 – Administrator guide**'],
    ['Something does not work', '**Part 4 – Troubleshooting**'],
  ]),

  // ------------------------------------------------------------------ PART 1
  H1('Part 1 – Setup steps'),
  P('Do these steps **once**, on the PC that will run the program (the “server PC”). It can be any Windows PC that is switched on during working hours.'),
  H3('What you need'),
  UL([
    'A Windows 10 or 11 PC (the server PC).',
    'Python 3.9 or newer – free from python.org (Step 2).',
    'Permission to **write** to the shared folder `' + SERVER + '`.',
    'Internet access on the very first start (to download Flask, about 10 MB). After that, no internet is needed.',
    'The ZIP file **NBE-CallLog.zip** you received.',
  ]),

  H2('Step 1 – Copy the program to the shared folder'),
  OL([
    'Unzip **NBE-CallLog.zip** (right-click the file, then **Extract All…**).',
    'Open the folder named `program`.',
    'Select **everything inside** `program` (Ctrl+A) and copy it.',
    'In Windows Explorer click the address bar, type `' + SERVER + '` and press Enter. Paste the files there.',
    'Check: the file `run.py` must be directly inside `calllog` – not inside another folder.',
  ]),
  IMG('setup-02-folder-contents.png', 'What the shared folder must contain after Step 1'),
  WARN('Copy the **contents** of `program`, not the folder `program` itself. If you see `' + SERVER + '\\program\\run.py`, move the files up one level.'),

  H2('Step 2 – Install Python on the server PC'),
  OL([
    'Open **python.org**, go to **Downloads** and download the newest Python 3 for Windows.',
    'Start the installer. On the first screen **tick “Add python.exe to PATH”**, then click **Install Now**.',
    'To check: press the Windows key, type `cmd`, press Enter. In the black window type `python --version` and press Enter. You should see a version such as `Python 3.12.4`.',
  ]),
  NOTE('If Windows says “python is not recognized”, run the installer again, choose **Modify** and make sure the PATH box is ticked, or restart the PC.'),

  H2('Step 3 – First start'),
  P('Open **Command Prompt** (Windows key, type `cmd`, Enter) and type:'),
  CODE('python ' + SERVER + '\\run.py --share'),
  P('Press Enter. The first start downloads Flask into the folder `libs` inside the shared folder – wait one or two minutes. Then the window shows the program is running:'),
  IMG('setup-03-first-run-window.png', 'The program window after a successful start', '**1** = the administrator password (shown only once) · **2** = the address for the other PCs'),
  UL([
    '**Write down the administrator password.** The User ID is `admin`. It is shown only once (if you lose it, see Part 4).',
    'Windows may ask to **allow python.exe through the firewall**. Choose **Allow access**. If you are not allowed to, ask your IT department to open **TCP port 8080** on this PC.',
    '**Keep this window open.** Closing it stops the program (no data is lost). Step 7 shows how to run it automatically.',
  ]),
  TIP('You can also just double-click `run.py` in the shared folder. That starts the program for **this PC only**; the `--share` word is what lets the other PCs connect.'),

  H2('Step 4 – Sign in as administrator'),
  OL([
    'On the server PC open a browser and go to `http://localhost:8080`.',
    'Enter User ID `admin` and the password from the window. Click **Sign in**.',
    'The program asks you to choose your own password: enter the old password, then the new one twice (at least 8 characters) and click **Save password**.',
    'You now see the administrator dashboard (Part 3).',
  ]),

  H2('Step 5 – Create the employee accounts'),
  P('Open the **Employees** tab. Use **Add employee** for one person or **Import from Excel / CSV** for many (see Part 3). Every employee gets a temporary password and must choose their own the first time they sign in.'),

  H2('Step 6 – Tell the employees the address'),
  UL([
    'The address is `http://<server PC name or IP>:8080`, for example `http://192.168.1.20:8080`. The program window prints it after **“Other PCs open:”**.',
    'To find the IP address of the server PC: open Command Prompt, type `ipconfig` and read **IPv4 Address**.',
    'Ask each employee to **bookmark** the address. Print the **Employee Quick Card** and write the address on it.',
    'If an employee cannot open the address, see Part 4 (firewall).',
  ]),

  H2('Step 7 – Keep it running automatically (Windows Task Scheduler)'),
  P('Without this step the program stops when the server PC restarts or when the black window is closed. The Windows Task Scheduler starts it at every start-up, **restarts it if it stops**, and **restarts it by itself about 30 seconds after you copy a newer version of the program** into the shared folder – so the running copy is always up to date. It also writes a log file `server.log` next to `run.py`.'),
  P('Do Step 3 once by hand first, sign in as `admin` and change the password. Then close the black window and set up the task: press the Windows key, type **Task Scheduler**, open it and choose **Action → Create Task…** (not “Create Basic Task”).'),
  TABLE([1700, 8006], ['Tab', 'What to set'], [
    ['General', 'Name: `NBE Call Log`.\nSelect **Run whether user is logged on or not**.\nDo **not** tick “Do not store password” – Windows needs your password to reach the shared folder. The account must have **write access** to `' + SERVER + '`.'],
    ['Triggers', 'Click **New…**. Begin the task: **At startup**. Tick **Delay task for** and choose **1 minute**. Click OK.'],
    ['Actions', 'Click **New…**. Action: **Start a program**.\n**Program/script:** the full path of `python.exe` (to find it: Command Prompt, type `where python`), for example `C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python312\\python.exe`.\n**Add arguments:** `"' + SERVER + '\\run.py" --service` (keep the quotation marks).\n**Start in:** leave **empty**.'],
    ['Conditions', 'Untick **Start the task only if the computer is on AC power** (laptops).\nTick **Start only if the following network connection is available** and choose **Any connection**.'],
    ['Settings', 'Tick **If the task fails, restart every** and choose **1 minute**; **Attempt to restart up to** 999 times.\n**If the task is already running, then the following rule applies:** **Do not start a new instance**.\nUntick **Stop the task if it runs longer than**.'],
  ]),
  P('Click **OK** and enter your Windows password when asked. Then **right-click the task → Run**, wait a few seconds and open the program address in a browser. If something is wrong, open `' + SERVER + '\\server.log` and read the last lines.'),
  NOTE('To stop the program: right-click the task → **End**. To keep it stopped: right-click → **Disable**.'),

  H2('Step 8 – Updating the program'),
  OL([
    'When you receive a new ZIP, unzip it and copy everything inside `program` over the old files in `' + SERVER + '` (choose **Replace**).',
    '**Never delete or replace the folder `data`** – it holds all the calls. The ZIP does not contain it. Your own `config.json` is not in the ZIP either, so it is not overwritten.',
    'With Step 7 in place nothing else is needed: the program restarts itself within about 30 seconds. Without Task Scheduler, close the black window and start it again.',
  ]),

  H2('Two ways to run it'),
  TABLE([2000, 3853, 3853], ['', 'One server PC (recommended)', 'Everyone runs it'], [
    ['How', 'The server PC runs `run.py --service` (Step 7). Everybody opens `http://<server PC>:8080`.', 'Each employee double-clicks `run.py` in the shared folder. It opens their browser.'],
    ['Python needed on', 'Only the server PC', 'Every PC'],
    ['Employees need access to the shared folder', 'No', 'Yes (write access to `data`)'],
    ['Good for', 'About 60 employees at the same time', 'A few people, no server PC'],
  ]),
  NOTE('Several PCs may even run the program at the same time: the files are locked safely so two people saving at once cannot damage each other’s data.'),

  H2('Settings (optional): config.json'),
  P('The program works without any settings. To change something, copy `config.example.json` to `config.json` (next to `run.py`), edit it in Notepad and restart the program.'),
  TABLE([2300, 7406], ['Setting', 'Meaning'], [
    ['`port`', 'Web port. Default `8080`. Use `--port 8081` if 8080 is taken.'],
    ['`host`', '`"127.0.0.1"` = only this PC (default). `"0.0.0.0"` = other PCs too (same as `--share`).'],
    ['`data_dir`', 'Where the data is kept. Default `"data"` = the folder `data` next to `run.py`. A full path also works.'],
    ['`user_edit_days`', '`null` = employees can edit or delete their own calls of any day. A number (for example `7`) limits it to the last N days. Administrators are never limited.'],
    ['`session_hours`', 'Automatic sign-out after this many hours. Default 12.'],
    ['`ip_phone_regex`', 'What counts as a valid IP phone number. Default: 3 to 10 digits.'],
    ['`threads`', 'How many requests are handled at the same time. Default 16.'],
  ]),
  H2('Backup'),
  P('Copy the folder `' + SERVER + '\\data` to a safe place (for example once a day). To restore, copy it back while the program is stopped.'),

  // ------------------------------------------------------------------ PART 2
  H1('Part 2 – Employee guide'),
  P('This part is for employees who answer calls. Printing the one-page **Employee Quick Card** is enough for daily work.'),
  H2('Sign in'),
  OL([
    'Open the address your administrator gave you (for example `http://192.168.1.20:8080`). Bookmark it.',
    'Type your **User ID** (your employee number) and your **Password**, then click **Sign in**.',
    'The first time you will be asked to choose your own password: enter the temporary password, then your new password twice (at least 8 characters) and click **Save password**.',
  ]),
  IMG('emp-01-login.png', 'The sign-in page'),
  IMG('emp-10-password.png', 'Choosing your own password (first sign-in only)'),
  NOTE('Forgot your password? Ask your administrator to reset it. After 5 wrong passwords you must wait 10 minutes.'),

  H2('Your screen'),
  IMG('emp-02-dashboard.png', 'The employee dashboard', '**1** = your name, ID and the day · **2** = Log a call · **3** = your numbers · **4** = calls by hour'),
  UL([
    '**Log a call** (left) is where you record each call.',
    'The **number tiles** show your calls today and how many were Resolved, Transferred or Ticket Inquiry.',
    '**Calls by hour** shows when you took your calls. Move the mouse over a bar for details, or click **View as table**.',
    'Further down: **Calls by reason** and the list **Today’s calls**.',
  ]),

  H2('Log a call'),
  P('Record every call right after you answer it. It takes four steps:'),
  OL([
    '**IP phone.** Click the first box and type the number of the IP phone that received the call (digits only, for example `75942`). Arabic digits are converted automatically.',
    '**Branch** – only if the caller is a branch. Type the branch **number** (for example `100`) or a part of its **name**. A list appears; click the branch you want, or press **Tab** or **Enter** to take the first one. The box then shows the number and name.',
    '**Caller is not a branch?** If the caller is a sector, a department or anyone else who is not a branch, **leave the Branch box empty**. Just enter the IP phone.',
    '**Reason.** Click the button that describes the call. **The call is saved immediately** when you click.',
  ]),
  IMG('emp-03-ip-and-branch.png', 'Typing the IP phone and searching for the branch', '**1** = IP phone · **2** = Branch box · **3** = the suggestion'),
  IMG('emp-04-reasons.png', 'Branch chosen – now click the reason', '**1** = the chosen branch (click × to remove it) · **2** = the four reasons'),
  TABLE([3700, 2800, 3206], ['Reason button', 'Counted as', 'Use it when…'], [
    ['تم الرد على الاستفسار\nInquiry answered', 'Resolved', 'you answered a question'],
    ['تم حل المشكلة\nProblem solved', 'Resolved', 'you fixed the problem during the call'],
    ['تم التحويل للجهة المختصة\nTransferred to the competent party', 'Transferred', 'you passed the call to another team'],
    ['متابعة طلب قائم\nFollow-up on an existing request', 'Ticket Inquiry', 'the caller asks about an existing request'],
  ]),
  P('**Optional note:** before you click the reason, click **Add a note** and type up to 300 characters.'),
  IMG('emp-05-saved.png', 'After saving: a message and the new call at the top of the list', '**1** = confirmation message · **2** = the call you just saved'),
  P('The form clears and the cursor goes back to the IP phone box, ready for the next call.'),
  TIP('**Fast way, only the keyboard:** type the IP phone → press **Enter** (moves to Branch) → type the branch number → press **Tab** → click the reason.'),
  TABLE([4200, 5506], ['Message', 'What it means'], [
    ['Enter the IP phone number first.', 'The IP phone box is empty.'],
    ['IP phone must be digits only, for example 75942.', 'Use digits only (3 to 10 of them).'],
    ['Choose the branch from the list, or clear the field if the caller is not a branch.', 'You typed text in the Branch box but did not pick a branch. Pick one from the list or click ×.'],
  ]),

  H2('Your calls'),
  IMG('emp-06-calls-list.png', 'Your list of calls', '**1** = search · **2** = download Excel or CSV · **3** = Edit · **4** = Delete'),
  UL([
    'The newest call is at the top. Each row shows the time, IP phone, branch, reason, status and note.',
    '**Search** finds a call by IP phone, branch number or name, or a word from the note.',
    '**Download** gives your own calls as an Excel or CSV file – for **this day** or **this month**.',
  ]),
  H3('Correct a mistake: Edit'),
  P('Click **Edit** on the row, change the IP phone, branch, reason or note and click **Save changes**.'),
  IMG('emp-07-edit.png', 'Editing a call', '**1** = IP phone · **2** = Branch · **3** = reason · **4** = Save changes'),
  H3('Remove a call: Delete'),
  P('Click the red bin button on the row and confirm with **Delete call**.'),
  IMG('emp-08-delete.png', 'Deleting a call asks for confirmation'),
  NOTE('Every change and deletion is recorded in the audit log together with your name, the time and the old and new values. If a row says **locked**, your administrator has closed editing for that day – ask them to correct it.'),

  H2('Other days'),
  P('Use the arrows next to the date, or click the date box, to look at an earlier day. **Today** brings you back. The **Log a call** box is only available for today.'),
  IMG('emp-09-dates.png', 'Looking at yesterday', '**1** = previous / next day · **2** = date box · **3** = Today'),
  H2('Sign out'),
  P('Click **Sign out** (top right) when you leave your PC. The program also signs you out automatically after 12 hours. Click **Password** at any time to change your password.'),
  H2('Questions and answers'),
  TABLE([3900, 5806], ['Question', 'Answer'], [
    ['I chose the wrong reason.', 'Click **Edit** on that row, choose the right reason, **Save changes**.'],
    ['I saved the same call twice.', 'Delete one of them with the bin button.'],
    ['The branch is not in the list.', 'Leave the Branch box empty, write the branch in the note and tell your administrator.'],
    ['The caller is not a branch.', 'Leave the Branch box empty. That is correct.'],
    ['My numbers do not change.', 'The page refreshes by itself every 20 seconds, and at once when you save. Press F5 to reload.'],
    ['It says my session expired.', 'Reload the page (F5) and sign in again.'],
  ]),

  // ------------------------------------------------------------------ PART 3
  H1('Part 3 – Administrator guide'),
  P('Administrators sign in with their own User ID (the first one is `admin`). An administrator account is for supervising: it does not log calls. A supervisor who also answers calls needs a second, normal employee account.'),
  H2('The dashboard'),
  IMG('adm-01-overview.png', 'The Overview tab', '**1** = tabs · **2** = date range · **3** = Live indicator · **4** = number tiles · **5** = calls by hour'),
  UL([
    '**Tabs:** Overview, Records, Employees, Branches, Audit log, Export.',
    '**Date range** (Today, Yesterday, Last 7 days, This month, Custom): one choice applies to **all** tabs except Employees and Branches.',
    '**Live:** the open tab refreshes itself – every 5 seconds for today, less often for longer periods, rarely for past days. The time shows the last update.',
  ]),
  H2('Overview'),
  IMG('adm-02-overview-people.png', 'Who called, from where', '**1** = Employees · **2** = Calls by reason · **3** = Calls by region · **4** = Top branches · **5** = Latest calls'),
  UL([
    '**Number tiles:** total calls, Resolved, Transferred, Ticket Inquiry and how many employees took calls.',
    '**Employees:** calls per employee with first and last call. Click a column title to sort. **Click a row** to see that employee’s calls. Employees with no calls are shown in grey.',
    '**Calls by reason**, **Calls by region** and **Top branches** show where the calls came from. Calls from sectors or other non-branch callers are counted in a note under the branches.',
    '**Latest calls:** the ten newest calls, updated live.',
  ]),
  H2('Records'),
  IMG('adm-03-records.png', 'The Records tab', '**1** = filters · **2** = Excel / CSV export of what you see · **3** = Branch · **4** = Edit / Delete'),
  UL([
    'Filter by **employee**, **department**, **region**, **reason**, or search for an IP phone, branch, note or name.',
    'You can **Edit** or **Delete** any call. Changes are written to the audit log.',
    '**Excel** and **CSV** download exactly the records that match the current filters and date range.',
  ]),
  H2('Employees'),
  IMG('adm-04-employees.png', 'The Employees tab', '**1** = Add employee · **2** = Import from Excel / CSV · **3** = Reset password · **4** = Disable'),
  H3('Add one employee'),
  OL([
    'Click **Add employee**.',
    'Enter the **User ID** (the employee number), **Full name** and **Department**. Choose **Role**: Employee (logs calls) or Administrator. Leave **Initial password** empty to have one generated.',
    'Click **Create account**. A window shows the **temporary password – once**. Give it to the employee.',
  ]),
  IMG('adm-05-add-employee.png', 'Add employee'),
  IMG('adm-06-temp-password.png', 'The temporary password is shown only once', '**1** = the temporary password'),
  H3('Add many employees: import'),
  OL([
    'Prepare an Excel or CSV file. The **first row** must contain the column titles `user_id`, `name`, `department` (and optionally `password`).',
    'Click **Import from Excel / CSV**, choose the file and click **Import**.',
    'Click **Download passwords (CSV)** to get the list of temporary passwords. It is shown only now – keep the file safe and delete it after handing out the passwords.',
  ]),
  TABLE([1800, 2200, 2200, 3506], ['user_id', 'name', 'department', 'password (optional)'], [
    ['230966', 'خالد محمد فكرى', 'Identity', '(leave empty)'],
    ['230967', 'سارة أحمد', 'Cards Support', 'Start2026'],
  ]),
  IMG('adm-07-import-employees.png', 'Import employees'),
  H3('Reset a password, disable, edit'),
  UL([
    '**Reset password:** creates a new temporary password (shown once). The old password stops working immediately.',
    '**Disable:** the employee is signed out at once and cannot sign in. Their recorded calls are kept. **Enable** brings the account back. Disable people who leave.',
    '**Edit:** change the name, department or role.',
    'You cannot disable or demote your own account, and at least one administrator must stay active.',
  ]),
  H2('Branches'),
  IMG('adm-08-branches.png', 'The Branches tab', '**1** = search · **2** = Import from Excel'),
  P('This is the list employees choose from when the caller is a branch. It contains the branch number, name, region and type. Use the search box to find a branch.'),
  H3('Replace the branch list'),
  OL([
    'Click **Import from Excel** and choose the file. The first row needs the titles `رقم الفرع` (branch number) and `اسم الفرع` (branch name); `اسم المنطقة التابع لها` (region) and `نوع الفرع` (type) are optional. English titles also work.',
    'Click **Replace the list**. The whole list is replaced.',
  ]),
  IMG('adm-09-import-branches.png', 'Importing a new branch list'),
  NOTE('Calls that were already logged **keep the branch they were logged with**, so changing the list never changes old reports. If the same number and name appear twice in the file they are merged; if the same number exists in two regions, both are offered with the region shown.'),
  H2('Audit log'),
  IMG('adm-10-audit.png', 'The Audit log tab', '**1** = filters · **2** = an edited call (old → new) · **3** = Excel / CSV'),
  P('The audit log records **who did what and when**: every sign-in and failed sign-in, every call created, edited or deleted (with the old and new values – deleted calls are kept in the log), account changes, branch imports and exports. It cannot be edited from the program. Filter by user or action, or search; **Excel** and **CSV** export the log for the selected dates.'),
  H2('Export'),
  IMG('adm-11-export.png', 'The Export tab', '**1** = the date range used · **2** = Full report'),
  TABLE([2400, 7306], ['Export', 'What you get'], [
    ['**Full report** (Excel)', 'One workbook: **Summary** (per employee), **By branch** (calls per branch) and **Calls** (every call).'],
    ['**All calls** (Excel or CSV)', 'A flat list: date, time, employee, department, IP phone, branch, region, reason (Arabic and English), status, note.'],
    ['**Audit log** (Excel or CSV)', 'The audit log for the date range.'],
  ]),
  P('Choose the dates first (Today, Last 7 days, This month or Custom). Arabic text opens correctly in Excel.'),
  H2('Reasons'),
  P('The four reasons come from the original Excel sheet. To rename one, or change which status it counts as (`resolved`, `transferred` or `ticket_inquiry`), open `' + SERVER + '\\data\\reasons.json` in Notepad, edit it and save. Employees see the change when they reload the page.'),
  H2('Where the data is'),
  TABLE([4300, 5406], ['File or folder (inside `data`)', 'Contents'], [
    ['`users.json`', 'Accounts. Passwords are stored scrambled and cannot be read.'],
    ['`reasons.json`, `branches.json`', 'The two drop-down lists.'],
    ['`records\\2026-10-11\\230966.json`', 'One file per employee per day.'],
    ['`logs\\audit-2026-10-11.jsonl`', 'The audit log, one line per event.'],
  ]),
  WARN('Anyone who can write to the shared folder can change the program or the data. Give that permission to administrators and the server account only. With **one server PC** employees need no access to the folder at all.'),

  // ------------------------------------------------------------------ PART 4
  H1('Part 4 – Troubleshooting'),
  TABLE([3300, 6406], ['Problem', 'What to do'], [
    ['Other PCs cannot open the address', 'Start the program with `--share` (Step 3 or Step 7). Allow **TCP port 8080** in the Windows Firewall of the server PC (ask IT). Check the address with `ipconfig`.'],
    ['“Cannot use the data folder…”', 'The shared folder is not reachable or the account cannot write to it. Open `' + SERVER + '` in Explorer first, sign in if asked, and try again.'],
    ['“Port 8080 is already in use”', 'Another program uses the port. Start with `--port 8081` (and give employees the new address).'],
    ['“python is not recognized”', 'Python is not installed or PATH was not ticked. See Step 2.'],
    ['The packages cannot be installed', 'The first start needs internet (or your company proxy). If the folder `libs` is half-filled, delete it and start again.'],
    ['I lost the administrator password', 'On the server PC open Command Prompt and type:\n`python ' + SERVER + '\\manage.py reset-password admin`\nIt prints a new temporary password.'],
    ['An employee cannot sign in', 'Wrong password, account disabled (Employees tab → Enable) or 5 wrong tries – wait 10 minutes, or reset the password.'],
    ['The program stopped when I closed the window', 'That is normal without Task Scheduler. Set it up (Step 7).'],
    ['It does not start after a reboot', 'Open `server.log`. Often the network is not ready yet – the program keeps trying by itself. Check that the task runs with a password and has **Delay task for 1 minute**.'],
    ['Numbers on another PC look a few seconds old', 'Normal on shared folders (up to about 10 seconds). Press F5.'],
  ]),
  GAP(),
  P('**Need to change something in the program?** Replace the files as in Step 8. Keep the folder `data`.'),
]

module.exports = {
  lang: 'en', title: 'NBE Call Log – User Guide', footer: 'NBE Call Log · User Guide', labels: { note: 'Note:', tip: 'Tip:', warn: 'Important:', figure: 'Figure', page: 'Page' }, blocks,
}

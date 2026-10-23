# Pinakeys 1.0.2 — first use

1. Install Python 3.10 or newer on this computer. Desktop use also requires
   Tkinter. Run `python3 -m tkinter` (Windows: `py -3 -m tkinter`) to check it.
   No extra packages are required to create an offline inventory.
2. Unzip the empty kit. Copy its four visible top-level items into the common
   parent of your material folders on the same drive. They are Open Library.html,
   Library Tools, Content and Incoming Files. **Do not overwrite an existing kit.**
   Existing collections can stay exactly where they are. Content and Incoming
   Files are optional workflow folders, not required homes for your books.
3. Open Library Tools → Mac → Pinakeys.app. Windows/Linux launchers are present
   but experimental. If the unsigned Mac app is blocked, open Terminal, type
   `sh `, drag `Library Tools/Mac/Update Catalogue.command` into it, then append
   ` --folder 'Your collection folder'` and press Return. This is a quick update.
4. In the app choose Folders and exclusions. Add your existing folders inside
   the kit's common parent, remove unwanted defaults and save. Optional exclusions
   include a camera folder or `*.zip`. No material is moved.
5. Choose Refresh Catalogue, then Open Library. Search and click a title to open
   the original offline. If a restricted viewer blocks local links, use a desktop
   browser, the simple HTML view, or the CSV/text export to find the path.
6. Choose Review Files and untick Needs review only if needed. Title/author boxes
   offer existing values. Edit and save. Corrections stay separate from the file.
   Only that file is fingerprinted when saving; the quick refresh is inexpensive.
7. After external renames/moves, use Full verification to reconnect unique copies.
   It reads the selected folders completely, has live progress and can be stopped.
   Cached sizes/timestamps are not proof of unchanged contents. Identical copies
   may need an explicit identity choice in Intake.
8. Keep Settings and Corrections with the kit and back them up. Restore records
   from backup can load Record History or a full JSON export without moving books.

Check Incoming, optional tool installation, metadata extraction, PDF follow-up,
previewed filing and undo are separate actions described in README. Nothing
starts a security check merely by opening the app or catalogue. Completed checks
never certify safety. Do not test installations against your only material copy.

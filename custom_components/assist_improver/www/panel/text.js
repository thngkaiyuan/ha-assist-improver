// The panel's words: TEXT, and the tables that name a run's stages, the pipeline's steps and each improvement's status.

export const STAGES = [["triage", "Triage"], ["analyze", "Analyze"], ["propose", "Propose"], ["write", "Write up"]];
// Every label and tooltip the settings view shows, in one place: edit the words here. {name}-style parts are filled
// in when shown.
export const TEXT = {
  steps: {
    requests: {title: "Your Assist", tip: "Your conversations with Assist, recorded as you use it. They wait here until the next run takes them in. The assistant shown is the one Assist uses by default."},
    triage: {title: "Triage", tip: "Recommended to use a small model to reduce downstream costs by filtering out conversations that clearly need no improvement."},
    analyze: {title: "Analyze", tip: "Reads each conversation to identify your intents, and where Assist could've done better."},
    propose: {title: "Propose", tip: "Propose changes to make your Assist experience smoother."},
    review: {title: "Review", tip: "Align proposals with the broader view of how Assist is used in your household."},
    test: {title: "Simulate", tip: "Replay your conversations with proposed fixes in a simulated copy of your home. Nothing in your home moves."},
    write: {title: "Write up", tip: "Packages technical improvements so they're easy for you to review."},
    you: {title: "Your review", tip: "Accept, decline, or ask for a revision."},
    household: {title: "Your household", tip: "Your house, made easier."},
  },
  onDemand: {label: "On-demand improvements",
             tip: "When on, ask Assist to file an improvement when something didn't go well: the improver studies it as soon as the conversation ends."},
  // Your conversations, on the home page: find them, read them, send them in.
  conversations: {title: "Your conversations", search: "Search what was said, or when", all: "All", today: "Today", week: "7 days",
                  month: "30 days", newest: "Newest", effort: "Most back-and-forth", oldest: "Oldest",
                  lead: "Pick the ones that didn't go the way you wanted, and run: the improver finds what went wrong and tests fixes.",
                  leadOnDemand: "Pick the ones that didn't go well, and run. Any you send in, or file with Assist, go straight to the improver.",
                  pick: "Select", pickOne: "Select “{said}”", selectAll: "Select all {n}",
                  // The selection's actions, at the bottom (Run is runOn): each with how many.
                  archiveMany: "Archive ({n})", unarchiveMany: "Unarchive ({n})", deleteMany: "Delete ({n})",
                  deleteAsk: "Delete {n} conversation{s} from Assist Improver? The improver can no longer check changes against {them}.",
                  clearSearch: "Clear search",
                  runOn: "Run on {n} conversation{s}", failedStep: "failed step", failedStepLabel: "Failed step:",
                  you: "You", assist: "Assist", send: "Send this one in", sending: "Sending…",
                  note: "What should have happened? (optional)", yourNote: "Your note:",
                  sentNow: "Sent in. The improver is looking at it now.", sentLater: "Sent in. It goes in with your next run.",
                  // Where each stands: the stage its flow record names, or its outcome (conversations.standing).
                  status: {waiting: "New", active: "Still going", sent: "Sent in", smooth: "Looked smooth", queued: "To analyze",
                           fine: "No trouble found", unclear: "Needs your note", working: "Looking for a fix",
                           writing: "Writing it up", review: "Up for review",
                           declined: "Declined", nofix: "No fix found", duplicate: "Already covered", improved: "Improved",
                           test: "Marked as a test", unreadable: "Couldn't be read", archived: "Archived"},
                  smoothTip: "Nothing to improve, at a quick look. Send it in for a full analysis.",
                  // A chip's tooltip, where it says what comes next; held, why (flow.py's words).
                  nofixTip: "No fix worked. Send it in again with a note for one more look.",
                  fineTip: "Nothing went wrong, as far as it could tell. Send it in again with a note if something did.",
                  unclearTip: "It couldn't tell what you wanted. Send it in again with a note.",
                  duplicateTip: "Another conversation's improvement covers it.", heldTip: "Held: {why}",
                  // The Status filter's button: everything, or what it shows (one status's name, or how many).
                  statusAll: "Status", statusSome: "Status: {n}",
                  more: "Show more", none: "No conversations match.", allArchived: "Nothing needs you right now.", nothing: "(nothing said)",
                  close: "No conversation has all those words. The closest:",
                  empty: "Use Assist as usual: your conversations show up here, ready to send in."},
  // An improvement's review: each test opens to its conversation as it went, and its replay with the change.
  review: {before: "Before", after: "With the change", stopped: "The replay stopped before Assist replied.",
           // What else an improvement gets you, beside new things Assist can do and fewer turns: side effects it no
           // longer leaves (each device, as it stays) and what you pointed out that it fixed (your words). {n}: how many.
           spared: {gain: "{n} unwanted change{s} avoided", title: "Unwanted changes avoided ({n})"},
           followed: {gain: "follows your feedback", title: "What you pointed out, now fixed ({n})"},
           // What you pointed out that a test doesn't show fixed, in your words: a section, each line's bubble (the
           // reply that still did it and why, or that it couldn't tell), and an amber chip on its test's row.
           open: {title: "Still open ({n})", reply: "Reply {n}: {why}",
                  setAside: "It didn't happen without the change either, so this test couldn't show it.",
                  chip: "{n} still open", chipTip: "Something you pointed out still happened in a reply with this change."},
           // A test whose replay made Assist noticeably wordier (counted by code): its chip and tooltip, and a line
           // under the tests when any did.
           moreWords: "Says more", moreWordsTip: "Assist's replies: about {after} words each, {before} before.",
           moreWordsLine: "With this change, Assist's replies got longer in {n} of these.",
           // A reason a decision can be given with: a box to tick under it, and the note's placeholder once ticked.
           reasons: {not_an_issue: {label: "Not an issue", hint: "It went fine, or you were only trying Assist out.",
                                    note: "Anything to add? (optional)"}}},
  // Privacy controls: proposed words, waiting for approval.
  privacy: {recording: "Record conversations", recordingTip: "When off, new Assist conversations aren't kept, so the improver can't learn from them.",
            people: "People",  // Above each person's switch, named as in Home Assistant.
            // An opened conversation on the home page: archive it (out of the list, kept for the improver), or delete it.
            archive: "Archive this conversation", unarchive: "Unarchive", showArchived: "Show archived ({n})",
            delete: "Delete from Assist Improver", cancel: "Cancel",
            more: "More actions", confirmDelete: "Delete this conversation from Assist Improver? The improver can no longer check changes against it."},
  sandbox: {label: "Sandbox mode", tip: "When turned on, your Assist conversations use a simulated copy of your home, so nothing real changes; the improver learns from them as practice. This applies only to your user.", others: "Also on for {names}."},
  autoAccept: {label: "Auto-accept low-risk changes",
               tip: "Automatically accepts Assist Improver's low-risk changes: household knowledge, and new names for devices other than locks, alarms and doors.",
               confirm: "Auto-accept low-risk changes?",
               confirmText: "Improvements that only add household knowledge lines or new device names will be applied without your review. Anything that lets Assist control more still waits for you. You'll be notified each time, and can undo.",
               on: "Turn on"},
  revise: {label: "Revisions", short: "Revisions"},
  block: {tip: "Stop this stage.", blocked: "Resume this stage."},
  badges: {new: "{n} new: the next run takes them in", waiting: "{n} held", held: "{n} held while blocked", review: "{n} to review", applied: "{n} applied"},
  assistant: {tip: "The assistant Assist uses by default", none: "Add an assistant in Settings → Voice assistants first."},
  simulate: {label: "Your assistant", tip: "At most {n} tests of a conversation each round, on your assistant.",
             per: "tests per conversation, each round",
             cost: "Each test replays one of your conversations with a fix, on your own assistant: {n} each round costs at most about {n} times that conversation, plus one replay of it without the fix"},
  passThrough: {label: "Pass through", tip: "No triage model: conversations pass straight on to analysis."},
  repliesOff: "Replies are checked with the write-up model when off",
  models: {large: "Needs judgement: a strong model works best.", small: "A small, quick model is enough.",
           missing: {label: "Not available", tip: "This AI model is gone from Home Assistant. Choose another."},
           unavailable: "This AI model is unavailable right now.", none: {label: "Choose…", tip: "This step needs an AI model."},
           weak: "Not recommended for this step", unknown: "Fit unknown",
           unknownTip: "Assist Improver can't tell which model this is. A strong model, such as Claude Opus 5.5, works best.",
           recommended: "Use recommended setup", recommendedBusy: "Setting up…",
           recommendedTip: "Claude Opus 5.5 for the steps that need judgement, a small model for Triage and Write up. AI Tasks that fit are reused; the rest are added to your AI provider."},
  // A full run (Settings' Run now, the schedule) takes in every conversation not analyzed yet; Run now says how many.
  schedule: {off: "Only when you run it", daily: "Every day at 3:00, on new conversations",
             weekly: "Every Sunday at 3:00, on new conversations"},
  runNow: "Run now", runAll: "Run on {n} new conversations", runOne: "Run on 1 new conversation", runOneStep: "Run one step",
  runWaiting: "Run what's waiting",  // A full run with no new conversation, but held ones to release.
  failure: {details: "Details", settings: "Open settings"},  // The home page's notice only when the household must act.
  confirm: {title: "Run Assist Improver?", all: "Run on {n} new conversations?", one: "Run on 1 new conversation?",
            on: "Run on {n} conversation{s}?", step: "Run {step}?", run: "Run", notNow: "Not now",
            // A run with new conversations names which, by their first words: up to three, then how many more; and
            // the held ones it releases, by the step that holds them, in the items' words.
            quoted: "“{said}”", which: "{names} and {n} more", also: "Also: {items}",
            cost: "Runs use your AI models, so they cost money.", waiting: "{items} {are} waiting.", more: "{items} and more are waiting.",
            // The cost line with a rough estimate from past runs under it (common.cost): one line per model, the
            // tokens a step takes on the model it runs on now. No prices.
            spend: {lead: "Runs use your AI models, so they cost money. Going by your past runs, about:",
                    analyze: "{tokens} tokens on {model}, to analyse {n} conversation{s}",
                    quick: "{tokens} tokens on {model}, for the quick check",
                    both: "{tokens} tokens on {model}, to analyse {n} conversation{s}, quick check included",
                    more: "More if it finds fixes to propose and test."},
            nothing: "Nothing is waiting right now.",
            blocked: "{steps} {is} blocked.", models: "{steps} can't run: {its} available. Choose another first.",
            its: "its AI model isn't", their: "their AI models aren't",
            items: {triage: "{n} conversation{s} to triage", analyze: "{n} conversation{s} to analyze",
                    propose: "{n} conversation{s} to propose fixes for", write: "{n} write-up{s}"},
            // Run one step: what's held at that step (it takes in nothing new).
            steps: {triage: "Triages {n} conversation{s} held there.", triageNone: "Nothing is held at Triage.",
                    analyze: "Analyzes {n} conversation{s}.", analyzeNone: "Nothing is held at Analyze.",
                    propose: "Proposes fixes for {n} conversation{s}.", proposeNone: "Nothing is held at Propose.",
                    write: "Writes up improvements for {n} conversation{s}.", writeNone: "Nothing is held at Write up."}},
  knowledge: {chip: "{n} learned", chipShort: "{n}", tip: "What your assistants know about your household. Accepted improvements add to it.",
              title: "Household knowledge", everyone: "Everyone", empty: "Nothing learned yet: accepted improvements add lines here.",
              search: "Search", none: "Nothing matches.", more: "Read more", less: "Show less", add: "Add a line",
              addFor: "Add a line just for {name}", placeholder: "One fact or preference, in a sentence", save: "Save", cancel: "Cancel",
              edit: "Edit", remove: "Delete", confirmRemove: "Delete this line?",
              // The one switch: whether every assistant on Assist reads the lines (settings.js, the Your household step).
              label: "Household knowledge", on: "Household knowledge on: your assistants read the {lines} learned so far.",
              off: "Household knowledge off: your assistants don't read what's learned."},
  // A run in progress, by which it is (common.runTitle), and where it is: the conversation it's on, and its stage.
  progress: {working: "Improving your assistant", all: "Running on {n} new conversations", one: "Running on 1 new conversation",
             picked: "Running on the conversations you picked", sent: "Running on what you sent in", step: "Running {step} only",
             revision: "Revising an improvement", conflict: "Resolving a conflict", at: "Conversation {k} of {total}, at {stage}."},
  history: {title: "History", triggers: {scheduled: "scheduled", filed: "on demand", picked: "picked", revision: "revision", conflict: "conflict"},
            more: "Show more", empty: "Nothing yet: runs and your decisions show up here.",
            run: {done: "Run finished", failed: "Run stopped early", cancelled: "Run cancelled", interrupted: "Run interrupted"},
            stopped: "Nothing was lost: what it didn't finish is held for your next run.", unfinished: "{steps} couldn't finish",
            setAside: "{n} couldn't be read",
            failed: "{n} held: {why}", addressed: "{n} already addressed by an improvement", posted: "Up for review", decisions: {accept: "Accepted", decline: "Declined", revise: "Asked for a revision",
            // A decision given with a reason reads by its reason.
            not_an_issue: "Declined: not an issue", undo: "Undid"}, auto: "Accepted automatically"},
};

// The settings view draws the pipeline as a track: requests merge onto the improvement loop (triage, analyze,
// propose, write up), a small loop joins the proposer and its reviewer, Simulate stamps what the proposer sends on,
// and you review at the far end; revisions go round again and what you accept peels off to the household. Each AI
// step names its model; the ones with a gate can be blocked (a run then stops there and what reaches it is held).
export const STEPS = {
  requests: {...TEXT.steps.requests, icon: "mdi:comment-processing-outline"},
  triage: {...TEXT.steps.triage, role: "triage", gate: "triage", icon: "mdi:filter-outline"},
  analyze: {...TEXT.steps.analyze, role: "analyzer", gate: "analyze", icon: "mdi:text-search"},
  propose: {...TEXT.steps.propose, role: "generator", gate: "propose", icon: "mdi:lightbulb-on-outline"},
  review: {...TEXT.steps.review, role: "reviewer", icon: "mdi:scale-balance"},
  test: {...TEXT.steps.test, icon: "mdi:flask-outline"},
  write: {...TEXT.steps.write, role: "editor", gate: "write", icon: "mdi:feather"},
  you: {...TEXT.steps.you, icon: "mdi:account-circle-outline"},
  household: {...TEXT.steps.household, icon: "mdi:home-heart"},
};
// The cards at work while a run's stage works (run.stage): Propose with its reviewer and its Simulate stamp (P0.4).
export const AT_WORK = {triage: ["triage"], analyze: ["analyze"], propose: ["propose", "review", "test"], write: ["write"]};
// The flow stage each card counts (the run preview's stages).
export const COUNTS = {requests: "Your Assist", triage: "Triage", analyze: "Analyze", propose: "Propose", write: "Write up",
                       you: "Your review"};
export const SCHEDULE = Object.entries(TEXT.schedule);
export const GATE_NAMES = Object.fromEntries(["triage", "analyze", "propose", "write"].map((g) => [g, STEPS[g].title]));
export const STATUS = {awaiting_review: "To review", needs_attention: "Needs attention", applied: "Applied", undone: "Undone",
  declined: "Declined", revised: "Replaced by a revision", conflict: "Changed since tested"};

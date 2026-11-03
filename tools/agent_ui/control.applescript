on run argv
    set operation to item 1 of argv
    set targetPID to item 2 of argv as integer
    tell application "System Events"
        set candidates to application processes whose bundle identifier is "com.vector35.binaryninja"
        if targetPID is not 0 then
            set candidates to application processes whose unix id is targetPID and bundle identifier is "com.vector35.binaryninja"
        end if
        if (count candidates) is 0 and operation is "inspect" then return ""
        if (count candidates) is not 1 then error "Select one Binary Ninja process with --pid."
        set targetProcess to item 1 of candidates
        tell targetProcess
            if operation is "inspect" then
                set documentURL to ""
                try
                    set documentURL to value of attribute "AXDocument" of window 1
                    if documentURL is missing value then set documentURL to ""
                end try
                return (unix id as text) & linefeed & documentURL
            end if
            set frontmost to true
            delay 0.4
            repeat 20 times
                if (count windows) > 0 then exit repeat
                delay 0.1
            end repeat
            if (count windows) is 0 then error "No accessible window for the selected Binary Ninja process."
            if operation is "save" then
                repeat with w in windows
                    if subrole of w is "AXDialog" or (count sheets of w) > 0 then
                        error "Resolve the open dialog before saving and restarting."
                    end if
                end repeat
                click menu item "Save All" of menu 1 of menu bar item "File" of menu bar 1
                delay 1
                repeat with w in windows
                    if subrole of w is "AXDialog" or (count sheets of w) > 0 then
                        error "Saving opened a dialog. Complete the save before restarting."
                    end if
                end repeat
            else if operation is "surface" then
                set actionName to item 3 of argv
                set pluginsMenu to menu 1 of menu bar item "Plugins" of menu bar 1
                set notesMenu to menu 1 of menu item "Notepad" of pluginsMenu
                click menu item actionName of notesMenu
            else if operation is "console" then
                set commandText to item 3 of argv
                set consoleInput to missing value
                repeat with attempt from 1 to 2
                    set lowestY to 0
                    set elements to entire contents of window 1
                    repeat with elementIndex from 1 to (count elements)
                        try
                            if role of item elementIndex of elements is "AXTextArea" then
                                set p to position of item elementIndex of elements
                                set dimensions to size of item elementIndex of elements
                                set promptText to ""
                                if elementIndex > 1 then
                                    try
                                        set promptText to value of item (elementIndex - 1) of elements as text
                                    end try
                                end if
                                if (promptText starts with ">>>" or promptText starts with "...") and item 2 of p > lowestY and item 2 of dimensions < 120 then
                                    set lowestY to item 2 of p
                                    set consoleInput to item elementIndex of elements
                                end if
                            end if
                        end try
                    end repeat
                    if consoleInput is not missing value then exit repeat
                    if attempt is 1 then
                        click menu item "Toggle Scripting Console" of menu "View" of menu bar 1
                        delay 0.4
                    end if
                end repeat
                if consoleInput is missing value then error "Python console input was not found."
                if value of consoleInput is not "" then error "Python console input is not empty."
                set value of attribute "AXFocused" of consoleInput to true
                set p to position of consoleInput
                set dimensions to size of consoleInput
                click at {(item 1 of p) + 20, (item 2 of p) + (item 2 of dimensions) / 2}
                delay 0.3
                set value of consoleInput to commandText
                delay 0.3
                if value of consoleInput is not commandText then
                    error "Console did not receive the complete command; it was not executed."
                end if
                -- AXValue can leave QTextEdit's caret at the start. Python's
                -- console treats Return there as a newline, not submission.
                set value of attribute "AXFocused" of consoleInput to true
                key code 124 using {command down}
                delay 0.1
                key code 36
            else
                error "Unknown UI control operation."
            end if
        end tell
    end tell
end run

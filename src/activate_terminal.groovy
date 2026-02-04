import com.intellij.openapi.actionSystem.ActionManager
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.openapi.project.ProjectManager

var actionManager = ActionManager.getInstance()

IDE.application.invokeLater {
    // Try to find the project matching CLAUDEQ_PROJECT_PATH if set
    var targetProject = null
    var projectPath = System.getenv("CLAUDEQ_PROJECT_PATH")

    System.err.println("=== ClaudeQ Project Matcher Debug ===")
    System.err.println("Target project path: " + projectPath)
    System.err.println("Available projects:")

    if (projectPath != null && !projectPath.isEmpty()) {
        // Find project with matching base path
        var allProjects = ProjectManager.getInstance().getOpenProjects()
        for (var i = 0; i < allProjects.length; i++) {
            var project = allProjects[i]
            var basePath = project.getBasePath()
            var projectName = project.getName()

            System.err.println("  [" + i + "] Name: '" + projectName + "'")
            System.err.println("      Path: '" + basePath + "'")
            System.err.println("      Match: " + (basePath != null && basePath.equals(projectPath)))

            if (basePath != null && basePath.equals(projectPath)) {
                targetProject = project
                System.err.println(">>> MATCHED! Using this project.")
                break
            }
        }
    }

    if (targetProject == null) {
        System.err.println(">>> No match found, using first project")
    }
    System.err.println("=====================================")
    System.err.flush()

    // Fallback to first open project if no match
    if (targetProject == null) {
        var openProjects = ProjectManager.getInstance().getOpenProjects()
        if (openProjects.length > 0) {
            targetProject = openProjects[0]
        }
    }

    if (targetProject != null) {
        // First, bring the project window to front
        import com.intellij.openapi.wm.WindowManager
        var windowManager = WindowManager.getInstance()
        var projectFrame = windowManager.getFrame(targetProject)

        if (projectFrame != null) {
            // Bring the window to front and focus it
            projectFrame.toFront()
            projectFrame.requestFocus()
        }

        // Then activate the Terminal tool window in that project
        var toolWindowManager = ToolWindowManager.getInstance(targetProject)
        var terminalWindow = toolWindowManager.getToolWindow("Terminal")

        if (terminalWindow != null) {
            // Show and activate the Terminal window
            terminalWindow.show(null)
            terminalWindow.activate(null)
        }
    }
}

import com.intellij.openapi.actionSystem.ActionManager
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.openapi.project.ProjectManager

var actionManager = ActionManager.getInstance()

IDE.application.invokeLater {
    // Try to find the project matching CLAUDEQ_PROJECT_PATH if set
    var targetProject = null
    var projectPath = System.getenv("CLAUDEQ_PROJECT_PATH")

    // Debug to file since stderr might not be captured
    var debugFile = new java.io.File(System.getProperty("user.home") + "/.claude-sockets/groovy-debug.log")
    var writer = new java.io.PrintWriter(new java.io.FileWriter(debugFile))

    writer.println("=== ClaudeQ Project Matcher Debug ===")
    writer.println("Target project path: " + projectPath)
    writer.println("Available projects:")

    if (projectPath != null && !projectPath.isEmpty()) {
        // Find project with matching base path
        var allProjects = ProjectManager.getInstance().getOpenProjects()
        for (var i = 0; i < allProjects.length; i++) {
            var project = allProjects[i]
            var basePath = project.getBasePath()
            var projectName = project.getName()

            writer.println("  [" + i + "] Name: '" + projectName + "'")
            writer.println("      Path: '" + basePath + "'")
            writer.println("      Match: " + (basePath != null && basePath.equals(projectPath)))

            if (basePath != null && basePath.equals(projectPath)) {
                targetProject = project
                writer.println(">>> MATCHED! Using this project.")
                break
            }
        }
    }

    if (targetProject == null) {
        writer.println(">>> No match found, using first project")
        var allProjects = ProjectManager.getInstance().getOpenProjects()
        if (allProjects.length > 0) {
            targetProject = allProjects[0]
        }
    }
    writer.println("=====================================")
    writer.close()

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

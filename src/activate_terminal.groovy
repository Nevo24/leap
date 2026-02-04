import com.intellij.openapi.actionSystem.ActionManager
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.openapi.project.ProjectManager

var actionManager = ActionManager.getInstance()

IDE.application.invokeLater {
    // Try to find the project matching CLAUDEQ_PROJECT_PATH if set
    var targetProject = null
    var projectPath = System.getenv("CLAUDEQ_PROJECT_PATH")

    if (projectPath != null && !projectPath.isEmpty()) {
        // Find project with matching base path
        for (project in ProjectManager.getInstance().getOpenProjects()) {
            var basePath = project.getBasePath()
            // Debug: print paths for comparison
            System.err.println("Checking project: " + project.getName() + " with path: " + basePath)
            System.err.println("Looking for: " + projectPath)

            if (basePath != null && basePath.equals(projectPath)) {
                targetProject = project
                System.err.println("Found matching project: " + project.getName())
                break
            }
        }
    }

    if (targetProject == null) {
        System.err.println("No matching project found, using first available")
    }

    // Fallback to first open project if no match
    if (targetProject == null) {
        var openProjects = ProjectManager.getInstance().getOpenProjects()
        if (openProjects.length > 0) {
            targetProject = openProjects[0]
        }
    }

    if (targetProject != null) {
        // Get the Terminal tool window for this project
        var toolWindowManager = ToolWindowManager.getInstance(targetProject)
        var terminalWindow = toolWindowManager.getToolWindow("Terminal")

        if (terminalWindow != null) {
            // Show and activate the Terminal window
            terminalWindow.show(null)
            terminalWindow.activate(null)
        }
    }
}

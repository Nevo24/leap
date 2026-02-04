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
        var allProjects = ProjectManager.getInstance().getOpenProjects()
        for (var i = 0; i < allProjects.length; i++) {
            var project = allProjects[i]
            var basePath = project.getBasePath()

            if (basePath != null && basePath.equals(projectPath)) {
                targetProject = project
                break
            }
        }
    }

    // Fallback to first project if no match
    if (targetProject == null) {
        var allProjects = ProjectManager.getInstance().getOpenProjects()
        if (allProjects.length > 0) {
            targetProject = allProjects[0]
        }
    }

    if (targetProject != null) {
        // Activate the Terminal tool window in the project
        var toolWindowManager = ToolWindowManager.getInstance(targetProject)
        var terminalWindow = toolWindowManager.getToolWindow("Terminal")

        if (terminalWindow != null) {
            // Show and activate the Terminal window
            terminalWindow.show(null)
            terminalWindow.activate(null)
        }
    }
}

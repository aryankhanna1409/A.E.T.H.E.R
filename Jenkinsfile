// AETHER CI/CD pipeline (declarative). Setup, agents and credentials: docs/CI_CD.md
//
//   every push and pull request   Lint and unit tests on a Linux agent (no model or GPU needed)
//   main                          + Windows installer on a Windows agent (when one is connected)
//                                 + GitHub release for the current Mark (only when the version changes)
//
// Agent labels: "linux" (the Docker agent next to the controller) and "windows" (a Windows 11 machine
// with Inno Setup 6). The Windows stage only runs when the global env var WINDOWS_AGENT_ENABLED is "true",
// so builds never sit waiting for a laptop that is switched off.

pipeline {
    agent none

    options {
        timestamps()
        buildDiscarder(logRotator(numToKeepStr: '30', artifactNumToKeepStr: '10'))
        disableConcurrentBuilds(abortPrevious: true)
        timeout(time: 60, unit: 'MINUTES')
    }

    parameters {
        booleanParam(name: 'BUILD_INSTALLER', defaultValue: false,
                     description: 'Also build the Windows installer on a branch other than main (needs the Windows agent).')
    }

    environment {
        GITHUB_REPO = 'aryankhanna1409/A.E.T.H.E.R'
        PIP_DISABLE_PIP_VERSION_CHECK = '1'
        PYTHONDONTWRITEBYTECODE = '1'
    }

    stages {
        stage('Lint and test') {
            agent { label 'linux' }
            stages {
                stage('Set up Python') {
                    steps {
                        sh '''
                            set -eu
                            python3 --version
                            rm -rf .venv reports
                            python3 -m venv .venv
                            .venv/bin/pip install --quiet --upgrade pip
                            .venv/bin/pip install --quiet -e ".[dev,documents]" numpy "ruff==0.17.6" pytest-cov
                        '''
                        script {
                            def mark = sh(script: '.venv/bin/python ci/release.py info', returnStdout: true).trim()
                            def info = mark.readLines().collectEntries { line -> def kv = line.split('=', 2); [(kv[0]): kv[1]] }
                            currentBuild.description = "${info.MARK_NAME} (${info.MARK_TAG})"
                        }
                    }
                }
                stage('Lint') {
                    steps {
                        sh '.venv/bin/ruff check --output-format=full .'
                    }
                }
                stage('Unit tests') {
                    steps {
                        sh '''
                            .venv/bin/python -m pytest -q -p no:cacheprovider \
                                --junitxml=reports/junit.xml \
                                --cov=aether --cov=ci --cov-report=xml:reports/coverage.xml --cov-report=term
                        '''
                    }
                    post {
                        always {
                            junit testResults: 'reports/junit.xml', allowEmptyResults: false
                            recordCoverage(tools: [[parser: 'COBERTURA', pattern: 'reports/coverage.xml']],
                                           sourceCodeRetention: 'NEVER')
                        }
                    }
                }
            }
            post {
                cleanup { deleteDir() }
            }
        }

        stage('Windows installer') {
            when {
                beforeAgent true
                allOf {
                    environment name: 'WINDOWS_AGENT_ENABLED', value: 'true'
                    anyOf { branch 'main'; expression { params.BUILD_INSTALLER } }
                }
            }
            agent { label 'windows' }
            options { timeout(time: 30, unit: 'MINUTES') }
            steps {
                powershell '& .\\scripts\\build_installer.ps1'
                archiveArtifacts artifacts: 'dist/AETHER-Setup-*.exe', fingerprint: true
                stash name: 'installer', includes: 'dist/AETHER-Setup-*.exe'
            }
            post {
                cleanup { deleteDir() }
            }
        }

        stage('Release Mark') {
            when {
                beforeAgent true
                branch 'main'
            }
            agent { label 'linux' }
            steps {
                script {
                    if (env.WINDOWS_AGENT_ENABLED == 'true') {
                        unstash 'installer'
                    }
                }
                withCredentials([usernamePassword(credentialsId: 'github-token',
                                                  usernameVariable: 'GITHUB_USER', passwordVariable: 'GITHUB_TOKEN')]) {
                    sh '''
                        set -eu
                        set --
                        for f in dist/AETHER-Setup-*.exe; do
                            if [ -f "$f" ]; then set -- "$@" --asset "$f"; fi
                        done
                        python3 ci/release.py publish --repo "$GITHUB_REPO" --commit "$GIT_COMMIT" "$@"
                    '''
                }
            }
            post {
                cleanup { deleteDir() }
            }
        }
    }
}

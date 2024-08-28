#!/usr/bin/env groovy

/* IMPORTANT:
 *
 * In order to make this pipeline work, the following configuration on Jenkins is required:
 * - slave with a specific label (see pipeline.agent.label below)
 * - credentials plugin should be installed and have the secrets with the following names:
 *   + lciadm100credentials (token to access Artifactory)
 */

def defaultBobImage = 'armdocker.rnd.ericsson.se/sandbox/adp-staging/adp-cicd/bob.2.0:1.5.2-0'
def bob = new BobCommand()
        .bobImage(defaultBobImage)
        .envVars([ISO_VERSION: '${ISO_VERSION}', ENM_ISO_REPO_VERSION: '${ENM_ISO_REPO_VERSION}'])
        .needDockerSocket(true)
        .toString()
def GIT_COMMITTER_NAME = 'enmadm100'
def GIT_COMMITTER_EMAIL = 'enmadm100@ericsson.com'
def failedStage = ''
pipeline {
    agent {
        label 'Cloud-Native'
    }
    parameters {
        string(name: 'ISO_VERSION', description: 'The ENM ISO version (e.g. 1.65.77)')
        string(name: 'SPRINT_TAG', description: 'Tag for GIT tagging the repository after build')
    }
    environment {
        GERRIT_HTTP_CREDENTIALS_FUser = credentials('FUser_gerrit_http_username_password')
    }
    stages {
        stage('Clean') {
            steps {
                deleteDir()
            }
        }
        stage('Inject Credential Files') {
            steps {
                withCredentials([file(credentialsId: 'lciadm100-docker-auth', variable: 'dockerConfig')]) {
                    //sh "install -m 600 ${dockerConfig} ${HOME}/.docker/config.json"
                    sh '''    
                        if [ ! -f ${HOME}/.docker/config.json ]; then
                            echo "File not found!  Installing permission change file"
                            install -m 600 ${dockerConfig} ${HOME}/.docker/config.json
                        else
                            echo "Config.json file exists . Moving to next stage"
                        fi
                    '''
                }
            }
        }
        stage('Checkout Base Image Git Repository') {
            steps {
                git branch: 'master',
                     credentialsId: 'enmadm100_private_key',
                     url: '${GERRIT_MIRROR}/OSS/ENM-Parent/SQ-Gate/com.ericsson.oss.containerisation/eric-enm-int-sysinfo-handler'
                sh '''
                    git remote set-url origin --push https://${GERRIT_HTTP_CREDENTIALS_FUser}@${GERRIT_CENTRAL_HTTP_E2E}/OSS/ENM-Parent/SQ-Gate/com.ericsson.oss.containerisation/eric-enm-int-sysinfo-handler
                '''
            }
        }
        stage('Linting Dockerfile') {
            steps {
                script {
                    sh "${bob} lint-dockerfile"
                    archiveArtifacts '*dockerfilelint.log'
                }
            }
            post {
                failure {
                    script {
                        failedStage = env.STAGE_NAME
                    }
                }
            }
        }
        }
        post {
        success {
            script {
                sh '''
                    set +x
                    echo "success"
                '''
            }
        }
        failure {
           script {
               sh '''
                   echo "Failed"
               '''
           }
        }
    }
}


// More about @Builder: http://mrhaki.blogspot.com/2014/05/groovy-goodness-use-builder-ast.html
import groovy.transform.builder.Builder
import groovy.transform.builder.SimpleStrategy

@Builder(builderStrategy = SimpleStrategy, prefix = '')
class BobCommand {
    def bobImage = 'bob.2.0:latest'
    def envVars = [:]
    def needDockerSocket = false

    String toString() {
        def env = envVars
                .collect({ entry -> "-e ${entry.key}=\"${entry.value}\"" })
                .join(' ')

        def cmd = """\
            |docker run
            |--init
            |--rm
            |--workdir \${PWD}
            |--user \$(id -u):\$(id -g)
            |-v \${PWD}:\${PWD}
            |-v /home/enmadm100/doc_push/group:/etc/group:ro
            |-v /home/enmadm100/doc_push/passwd:/etc/passwd:ro
            |-v \${HOME}/.m2:\${HOME}/.m2
            |-v \${HOME}/.docker:\${HOME}/.docker
            |${needDockerSocket ? '-v /var/run/docker.sock:/var/run/docker.sock' : ''}
            |${env}
            |\$(for group in \$(id -G); do printf ' --group-add %s' "\$group"; done)
            |--group-add \$(stat -c '%g' /var/run/docker.sock)
            |${bobImage}
            |"""
        return cmd
                .stripMargin()           // remove indentation
                .replace('\n', ' ')      // join lines
                .replaceAll(/[ ]+/, ' ') // replace multiple spaces by one
    }
}
# Jenkins controller: plugins and configuration baked in, no setup wizard.
# Everything it needs (admin user, credentials, agent, the AETHER job) comes from casc.yaml.
FROM jenkins/jenkins:2.580.3-lts-jdk21

ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false -Xms256m -Xmx768m" \
    CASC_JENKINS_CONFIG=/usr/share/jenkins/ref/casc.yaml

COPY --chown=jenkins:jenkins plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

COPY --chown=jenkins:jenkins casc.yaml /usr/share/jenkins/ref/casc.yaml

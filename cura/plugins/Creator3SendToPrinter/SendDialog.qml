import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import UM 1.5 as UM

UM.Dialog {
    id: dialog
    title: "Creator 3 - Send to printer"
    width: 850 * screenScaleFactor
    height: 610 * screenScaleFactor
    minimumWidth: 750 * screenScaleFactor
    minimumHeight: 560 * screenScaleFactor
    backgroundColor: UM.Theme.getColor("main_background")
    onVisibleChanged: manager.visibility(visible)
    property string cameraHost: visible && cameraEnabled.checked
        && printers.currentIndex >= 0 && printers.currentIndex < manager.printers.length
        ? manager.printers[printers.currentIndex].address : ""
    onCameraHostChanged: manager.camera.setHost(cameraHost)

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 12
        Label { text: "Target printer"; font.bold: true; color: UM.Theme.getColor("text") }
        RowLayout {
            Layout.fillWidth: true
            ComboBox {
                id: printers
                Layout.fillWidth: true
                model: manager.printers
                textRole: "label"
                currentIndex: manager.selectedIndex
                enabled: !manager.busy
                onActivated: manager.selectPrinter(currentIndex)
            }
            Button { text: "Discover"; enabled: !manager.busy; onClicked: manager.search() }
            Button { text: "Refresh"; enabled: !manager.busy; onClicked: manager.refreshStatus() }
            Button { text: "Remove"; enabled: !manager.busy && printers.count > 0; onClicked: manager.removePrinter(printers.currentIndex) }
        }
        ListView {
            Layout.fillWidth: true
            Layout.preferredHeight: 145
            clip: true
            model: manager.printers
            delegate: Rectangle {
                property bool hasProgress: typeof modelData.printProgress === "number"
                width: ListView.view.width
                height: 70
                color: UM.Theme.getColor(index === printers.currentIndex ? "setting_control_highlight" : "setting_control")
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 6
                    ColumnLayout {
                        Layout.preferredWidth: 250
                        Label { text: modelData.name; font.bold: true; color: UM.Theme.getColor("text") }
                        Label { text: modelData.address; color: UM.Theme.getColor("text") }
                    }
                    ColumnLayout {
                        Layout.preferredWidth: 180
                        Label { text: "L: " + modelData.left + " C   R: " + modelData.right + " C"; color: UM.Theme.getColor("text") }
                        Label { text: "Bed: " + modelData.bed + " C"; color: UM.Theme.getColor("text") }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        Label { text: modelData.state + (modelData.fresh ? "" : " (refresh needed)"); color: UM.Theme.getColor("text") }
                        ProgressBar { Layout.fillWidth: true; from: 0; to: 100; value: hasProgress ? modelData.printProgress : 0; visible: hasProgress }
                    }
                    Label { text: hasProgress ? modelData.printProgress.toFixed(1) + " %" : "--"; color: UM.Theme.getColor("text") }
                }
                MouseArea {
                    anchors.fill: parent
                    enabled: !manager.busy
                    onClicked: { printers.currentIndex = index; manager.selectPrinter(index); }
                }
                ToolTip.visible: hover.hovered && modelData.error !== ""
                ToolTip.text: modelData.error
                HoverHandler { id: hover }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            TextField { id: name; placeholderText: "Name (e.g. Workshop Creator 3)"; Layout.fillWidth: true; enabled: !manager.busy }
            TextField { id: ip; placeholderText: "IPv4 address"; Layout.preferredWidth: 150; enabled: !manager.busy }
            Button { text: "Add"; enabled: !manager.busy; onClicked: manager.addPrinter(name.text, ip.text) }
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            ColumnLayout {
                Layout.fillWidth: true
                Label { text: "Short, unique filename on the printer:"; font.bold: true; color: UM.Theme.getColor("text") }
                Label { text: manager.filename; Layout.fillWidth: true; elide: Text.ElideMiddle; color: UM.Theme.getColor("text") }
                Label {
                    text: "The active build plate is exported with Creator 3 post-processing.\nUploading alone does not start printing."
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    color: UM.Theme.getColor("text")
                }
            }
            ColumnLayout {
                Layout.preferredWidth: 240 * screenScaleFactor
                CheckBox {
                    id: cameraEnabled
                    text: "Camera preview"
                    checked: true
                    contentItem: Label {
                        text: cameraEnabled.text
                        leftPadding: cameraEnabled.indicator.width + cameraEnabled.spacing
                        verticalAlignment: Text.AlignVCenter
                        color: UM.Theme.getColor("text")
                    }
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 180 * screenScaleFactor
                    color: "black"
                    border.color: UM.Theme.getColor("text")
                    Image {
                        objectName: "cameraImage"
                        anchors.fill: parent
                        anchors.margins: 1
                        source: manager.camera.source
                        fillMode: Image.PreserveAspectFit
                        cache: false
                    }
                    Label {
                        anchors.fill: parent
                        anchors.margins: 8
                        visible: manager.camera.source === ""
                        text: !cameraEnabled.checked ? "Preview off"
                            : cameraHost === "" ? "Select a printer"
                            : manager.camera.error !== "" ? "Camera unavailable\n" + manager.camera.error
                            : "Connecting to camera..."
                        wrapMode: Text.Wrap
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        color: "white"
                    }
                }
            }
        }
        ProgressBar { from: 0; to: 100; value: manager.progress; Layout.fillWidth: true; visible: manager.busy }
        Label { text: manager.status; Layout.fillWidth: true; Layout.fillHeight: true; wrapMode: Text.Wrap; color: UM.Theme.getColor("text") }
        RowLayout {
            Layout.fillWidth: true
            Button { text: "Cancel operation"; visible: manager.busy; onClicked: manager.cancel() }
            Item { Layout.fillWidth: true }
            Button {
                text: "Send"
                enabled: !manager.busy && !manager.monitoring && printers.currentIndex >= 0 && printers.currentIndex < manager.printers.length && manager.printers[printers.currentIndex].ready
                onClicked: manager.send(printers.currentIndex)
            }
            Button { text: "Start print..."; enabled: manager.canStart; onClicked: confirmation.open() }
            Button { text: "Close"; enabled: !manager.busy; onClicked: dialog.hide() }
        }
        Dialog {
            id: confirmation
            objectName: "startConfirmation"
            title: "Confirm print start"
            modal: true
            width: Math.min(parent.width - 32, 580 * screenScaleFactor)
            anchors.centerIn: parent
            contentItem: Label {
                text: "Start " + manager.uploadedDescription + "?\nCheck the build plate, filament and both nozzles before confirming."
                wrapMode: Text.Wrap
            }
            footer: DialogButtonBox {
                Button { objectName: "confirmStartButton"; text: "Start print"; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole }
                Button { text: "Cancel"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            }
            onAccepted: manager.startPrint()
        }
    }
}

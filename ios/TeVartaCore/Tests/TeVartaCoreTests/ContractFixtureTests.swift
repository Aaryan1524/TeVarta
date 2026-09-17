import Foundation
import Testing
@testable import TeVartaCore

struct ContractFixtureTests {
    private func fixture(_ name: String) throws -> Data {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0 ..< 5 { root.deleteLastPathComponent() }
        return try Data(contentsOf: root.appending(path: "fixtures/v1/\(name)"))
    }

    @Test func sharedFixturesDecodeWithSwiftModels() throws {
        _ = try ContractDecoder.json.decode(AuthResponse.self, from: fixture("auth_success.json"))
        _ = try ContractDecoder.json.decode(EntitySearchResponse.self, from: fixture("entity_search_ambiguous.json"))
        _ = try ContractDecoder.json.decode(EntitySearchResponse.self, from: fixture("entity_search_success.json"))
        _ = try ContractDecoder.json.decode(SeedResponse.self, from: fixture("seed_success.json"))
        _ = try ContractDecoder.json.decode(SeedsStatusResponse.self, from: fixture("seeds_status.json"))
        _ = try ContractDecoder.json.decode(FeedResponse.self, from: fixture("feed_success.json"))
        _ = try ContractDecoder.json.decode(FeedResponse.self, from: fixture("feed_empty.json"))
        _ = try ContractDecoder.json.decode(LoadingResponse.self, from: fixture("loading.json"))
        _ = try ContractDecoder.json.decode(EventDetailResponse.self, from: fixture("event_success.json"))
        _ = try ContractDecoder.json.decode(EventDetailResponse.self, from: fixture("event_merged.json"))
        _ = try ContractDecoder.json.decode(GraphResponse.self, from: fixture("graph.json"))
        _ = try ContractDecoder.json.decode(EntityResponse.self, from: fixture("entity_success.json"))
        _ = try ContractDecoder.json.decode(ProfileResponse.self, from: fixture("profile.json"))
        _ = try ContractDecoder.json.decode(DeviceResponse.self, from: fixture("device_success.json"))
        _ = try ContractDecoder.json.decode(FeedbackResponse.self, from: fixture("feedback_success.json"))
        _ = try ContractDecoder.json.decode(AccountDeletionResponse.self, from: fixture("account_deletion.json"))
        _ = try ContractDecoder.json.decode(ErrorEnvelope.self, from: fixture("errors/expired_auth.json"))
    }

    @Test func mergedEventPreservesTheRequestedIDAndCanonicalEvent() throws {
        let merged = try ContractDecoder.json.decode(EventDetailResponse.self, from: fixture("event_merged.json"))

        #expect(merged.requestedEventId == "evt_fremont_initial")
        #expect(merged.event.eventId == "evt_fremont_halt")
        #expect(merged.mergedFrom == ["evt_fremont_initial"])
    }

    @Test func telemetryClientSendsTheAuthenticatedTelemetryEnvelope() async throws {
        let transport = RecordingFeedbackTransport(
            responseData: try fixture("feedback_success.json"),
            response: HTTPURLResponse(
                url: URL(string: "https://api.example.test/v1/feedback")!,
                statusCode: 202,
                httpVersion: nil,
                headerFields: nil
            )!
        )
        let client = TelemetryClient(
            baseURL: URL(string: "https://api.example.test/")!,
            accessToken: { "access-token" },
            transport: transport
        )
        let event = FeedbackRequest(
            eventId: "evt_test",
            action: .cardImpression,
            slotType: "graph",
            slotPosition: 1,
            occurredAt: "2026-09-13T00:00:00Z",
            idempotencyKey: "telemetry-key-1"
        )

        let result = try await client.record(event)

        #expect(result.accepted)
        #expect(transport.request?.url?.path == "/v1/feedback")
        #expect(transport.request?.value(forHTTPHeaderField: "Authorization") == "Bearer access-token")
        let sent = try ContractDecoder.json.decode(FeedbackRequest.self, from: transport.request!.httpBody!)
        #expect(sent == event)
    }

    @Test func telemetryClientRejectsMissingAccessToken() async {
        let client = TelemetryClient(
            baseURL: URL(string: "https://api.example.test/")!,
            accessToken: { nil },
            transport: RecordingFeedbackTransport(responseData: Data(), response: URLResponse())
        )

        await #expect(throws: TelemetryClientError.missingAccessToken) {
            try await client.record(
                FeedbackRequest(
                    action: .appOpen,
                    occurredAt: "2026-09-13T00:00:00Z",
                    idempotencyKey: "telemetry-key-1"
                )
            )
        }
    }

    @Test func telemetryClientExposesServerRejectionWithoutDroppingItsCode() async throws {
        let transport = RecordingFeedbackTransport(
            responseData: try fixture("errors/expired_auth.json"),
            response: HTTPURLResponse(
                url: URL(string: "https://api.example.test/v1/feedback")!,
                statusCode: 401,
                httpVersion: nil,
                headerFields: nil
            )!
        )
        let client = TelemetryClient(
            baseURL: URL(string: "https://api.example.test/")!,
            accessToken: { "expired-token" },
            transport: transport
        )

        await #expect(
            throws: TelemetryClientError.rejected(
                statusCode: 401,
                code: "expired_auth",
                message: "Your session has expired. Refresh your session and try again."
            )
        ) {
            try await client.record(
                FeedbackRequest(
                    action: .appOpen,
                    occurredAt: "2026-09-13T00:00:00Z",
                    idempotencyKey: "telemetry-key-1"
                )
            )
        }
    }
}

private final class RecordingFeedbackTransport: FeedbackTransport, @unchecked Sendable {
    private let responseData: Data
    private let response: URLResponse
    private(set) var request: URLRequest?

    init(responseData: Data, response: URLResponse) {
        self.responseData = responseData
        self.response = response
    }

    func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        self.request = request
        return (responseData, response)
    }
}

/* eslint-disable @typescript-eslint/no-empty-interface */
// Generated code for namespace: org.openclaw.consulting@1.0.0

// imports
import {IConcept} from './concerto@1.0.0';

// interfaces
export interface IConsultingData extends IConcept {
 agreementNo: string;
 signingPlace: string;
 effectiveDate: string;
 partyAName: string;
 partyAAddress: string;
 partyAContact: string;
 partyALegalRep: string;
 partyBName: string;
 partyBAddress: string;
 partyBLegalRep: string;
 partyBContact: string;
 partyBRoleDescription: string;
 targetCountry: string;
 consultingFeePercent: number;
 consultingFeeCap: string;
 paymentTermDays: number;
 confidentialityTermYears: number;
 agreementTermMonths: number;
 terminationNoticeDays: number;
 breachCurePeriodDays: number;
 tailPeriodMonths: number;
 localComplianceLaws?: string[];
 governingLaw: string;
 arbitrationBody: string;
 noticeChangeBusinessDays: number;
 bilingualEnglishPrevails: boolean;
}
